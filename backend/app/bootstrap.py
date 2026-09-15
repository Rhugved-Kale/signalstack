"""Populate an empty database on startup.

A fresh deployment points at a brand-new Neon database. Without this, the
first visitor gets a correctly-functioning dashboard showing nothing at all,
which looks broken. So on startup: if there are no campaigns, run the whole
pipeline once.

Two constraints shape the design:

* **It must not delay the port binding.** Render's health check fails the
  deploy if the server does not accept connections quickly, and a full
  pipeline run takes several seconds. So this runs as a background task after
  startup, never inline.
* **It must be idempotent.** A restart, a redeploy or a second worker must not
  wipe or duplicate existing data — it checks for campaigns and returns
  immediately if any exist.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import BOOTSTRAP_SEED, BOOTSTRAP_USERS, settings
from app.db.models import Campaign
from app.db.session import SessionLocal
from app.observability import peak_rss_mb

logger = logging.getLogger(__name__)

BootstrapState = Literal["pending", "running", "complete", "skipped", "failed"]


class BootstrapStatus:
    """Thread-safe holder for the bootstrap's state, surfaced by /health."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: BootstrapState = "pending"
        self._detail: str | None = None

    @property
    def state(self) -> BootstrapState:
        with self._lock:
            return self._state

    @property
    def detail(self) -> str | None:
        with self._lock:
            return self._detail

    def set(self, state: BootstrapState, detail: str | None = None) -> None:
        with self._lock:
            self._state = state
            self._detail = detail


status = BootstrapStatus()


def database_is_empty(db: Session) -> bool:
    """True when there is nothing to show on the dashboard."""
    return (db.scalar(select(func.count()).select_from(Campaign)) or 0) == 0


def run_bootstrap(
    *, seed: int = BOOTSTRAP_SEED, users: int = BOOTSTRAP_USERS
) -> BootstrapState:
    """Generate → ingest → replay → attribute, but only into an empty database.

    Imports the pipeline lazily so that merely importing this module (as
    `main` does at startup) does not pull in the generator and attribution
    engine before the server has bound its port.
    """
    from app.attribution.engine import run_attribution
    from app.generator.fake_apis import FailureConfig
    from app.generator.world import WorldConfig
    from app.pipeline.replay import replay_quarantine
    from app.pipeline.runner import run_ingestion

    started = time.perf_counter()
    db = SessionLocal()
    try:
        if not database_is_empty(db):
            logger.info("bootstrap: database already has data, skipping")
            status.set("skipped", "database already populated")
            return "skipped"

        logger.info("bootstrap: empty database — generating seed=%s users=%s", seed, users)
        status.set("running", f"generating {users} users")

        world_config = WorldConfig(seed=seed, n_users=users)
        failure_config = FailureConfig()

        ingest = run_ingestion(db, world_config=world_config, failure_config=failure_config)
        status.set("running", "replaying quarantine")
        replay = replay_quarantine(
            db, world_config=world_config, failure_config=failure_config
        )
        status.set("running", "scoring attribution")
        attribution = run_attribution(db, rebuild=True)

        elapsed = time.perf_counter() - started
        detail = (
            f"{ingest['totals']['ingested']} rows ingested, "
            f"{replay['totals']['recovered']} recovered, "
            f"{attribution['totals']['rows_written']} attribution rows "
            f"in {elapsed:.1f}s"
        )
        logger.info("bootstrap: complete — %s (peak RSS %.1f MB)", detail, peak_rss_mb())
        status.set("complete", detail)
        return "complete"
    except Exception as exc:  # noqa: BLE001 - a failed bootstrap must not kill the server
        db.rollback()
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("bootstrap failed")
        status.set("failed", message)
        return "failed"
    finally:
        db.close()


def maybe_bootstrap_in_background() -> None:
    """Kick the bootstrap onto a thread if it is enabled.

    A daemon thread rather than a task on the event loop: the pipeline is
    entirely synchronous and CPU/DB-bound, so running it on the loop would
    block every request for its duration.
    """
    if not settings.BOOTSTRAP_ON_EMPTY:
        logger.info("bootstrap: disabled by BOOTSTRAP_ON_EMPTY=false")
        status.set("skipped", "disabled by configuration")
        return

    thread = threading.Thread(
        target=run_bootstrap, name="signalstack-bootstrap", daemon=True
    )
    thread.start()
