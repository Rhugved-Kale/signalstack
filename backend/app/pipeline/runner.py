"""Ingestion orchestration: fetch -> validate -> route -> load.

Per source: open an `IngestionRun`, stream every page through the retry layer,
validate each record, send the good ones to a batch upsert and the bad ones to
`quarantined_records`, then close the run with its counters.

Design rules that fall out of "a single bad record must never abort the run":
  * Validation happens per record inside the streaming loop, so one rejection
    only costs that record.
  * Whatever was collected is still loaded even if a later page fails
    permanently — good data already paid for is never thrown away.
  * Each source commits on its own, so a late failure cannot undo earlier work.
  * Every source runs even if an earlier one blew up (campaigns excepted, which
    must come first because everything else references it).

Counter identity (exact, enforced by a test):

    records_received == records_ingested + records_quarantined

where `records_ingested` counts unique rows upserted PLUS in-batch duplicates
that were dropped. A dropped duplicate is counted as ingested because its data
*is* in the database — its twin put it there. The `rows_inserted`,
`rows_updated` and `duplicates_dropped` breakdown is returned separately for
anyone who needs the distinction (`IngestionRun` has no column for it).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from pydantic import BaseModel, ValidationError
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.db.models import IngestionRun, QuarantinedRecord
from app.generator.fake_apis import (
    FailureConfig,
    FakeAdPlatformAPI,
    FakeEventStreamAPI,
    FakePaymentAPI,
)
from app.generator.world import World, WorldConfig, build_world
from app.pipeline.fetcher import FetchStats, fetch_all_pages
from app.pipeline.loaders import (
    LoadResult,
    upsert_ad_spend,
    upsert_campaigns,
    upsert_conversions,
    upsert_touchpoints,
)
from app.pipeline.schemas import (
    AdSpendIn,
    CampaignIn,
    ConversionIn,
    TouchpointIn,
    describe_validation_error,
)

logger = logging.getLogger(__name__)

# Campaigns first: ad spend and touchpoints resolve campaigns by external_id,
# so ingesting them before campaigns exist would quarantine everything.
SOURCE_ORDER = ("campaigns", "ad_spend", "touchpoints", "conversions")

QUARANTINE_BATCH = 500


@dataclass(frozen=True)
class SourceSpec:
    """Everything needed to ingest one source."""

    name: str
    fetch_fn: Callable[..., dict[str, Any]]
    model: type[BaseModel]
    loader: Callable[[Session, Sequence[Any]], LoadResult]
    per_page: int


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def build_source_specs(
    world: World, failure_config: FailureConfig, seed: int
) -> dict[str, SourceSpec]:
    """Wire the fake APIs up to their schema and loader.

    Each API gets its own seed offset so the three of them do not replay an
    identical failure sequence.
    """
    ad_platform = FakeAdPlatformAPI(world, failure_config, seed=seed)
    event_stream = FakeEventStreamAPI(world, failure_config, seed=seed + 1)
    payments = FakePaymentAPI(world, failure_config, seed=seed + 2)

    return {
        "campaigns": SourceSpec(
            "campaigns", ad_platform.list_campaigns, CampaignIn, upsert_campaigns, 50
        ),
        "ad_spend": SourceSpec(
            "ad_spend", ad_platform.list_ad_spend, AdSpendIn, upsert_ad_spend, 500
        ),
        "touchpoints": SourceSpec(
            "touchpoints",
            event_stream.list_touchpoints,
            TouchpointIn,
            upsert_touchpoints,
            500,
        ),
        "conversions": SourceSpec(
            "conversions",
            payments.list_conversions,
            ConversionIn,
            upsert_conversions,
            500,
        ),
    }


def _write_quarantine(
    db: Session,
    run_id: int,
    source: str,
    rows: Sequence[tuple[dict[str, Any], str]],
) -> None:
    """Batch-insert quarantined records with their raw payload intact."""
    if not rows:
        return
    payloads = [
        {
            "ingestion_run_id": run_id,
            "source": source,
            "raw_payload": raw,
            "error_reason": reason,
        }
        for raw, reason in rows
    ]
    for start in range(0, len(payloads), QUARANTINE_BATCH):
        db.execute(insert(QuarantinedRecord), payloads[start : start + QUARANTINE_BATCH])


def _ingest_source(
    db: Session,
    spec: SourceSpec,
    *,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Run one source end to end and return its result summary."""
    started_at = _now()
    started_perf = time.perf_counter()

    # Commit the "running" row immediately so the attempt is on record even if
    # this process dies mid-source.
    run = IngestionRun(source=spec.name, started_at=started_at, status="running")
    db.add(run)
    db.commit()
    run_id = run.id

    stats = FetchStats()
    valid: list[Any] = []
    raw_by_model: dict[int, dict[str, Any]] = {}
    quarantine: list[tuple[dict[str, Any], str]] = []
    error_message: str | None = None
    fetch_failed = False

    fetch_kwargs: dict[str, Any] = {"per_page": spec.per_page}
    if sleep_fn is not None:
        fetch_kwargs["sleep_fn"] = sleep_fn

    try:
        for raw in fetch_all_pages(
            spec.fetch_fn, source=spec.name, stats=stats, **fetch_kwargs
        ):
            try:
                record = spec.model.model_validate(raw)
            except ValidationError as exc:
                quarantine.append((raw, describe_validation_error(exc)))
                continue
            valid.append(record)
            raw_by_model[id(record)] = raw
    except Exception as exc:  # noqa: BLE001 - one source must not kill the rest
        fetch_failed = True
        error_message = f"{type(exc).__name__}: {exc}"
        logger.warning("source=%s fetch failed: %s", spec.name, error_message)

    load = LoadResult()
    try:
        # Load whatever made it through, even if fetching stopped early.
        load = spec.loader(db, valid)
        for record, reason in load.rejected:
            quarantine.append((raw_by_model.get(id(record), {}), reason))
        _write_quarantine(db, run_id, spec.name, quarantine)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        error_message = f"{type(exc).__name__}: {exc}"
        logger.exception("source=%s load failed", spec.name)
        # The data is gone, but the quarantine reasons are worth keeping.
        load = LoadResult()
        try:
            _write_quarantine(db, run_id, spec.name, quarantine)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            quarantine = []
        fetch_failed = True

    ingested = load.affected + load.deduplicated
    quarantined = len(quarantine)

    if fetch_failed:
        status = "failed"
    elif quarantined:
        status = "partial"
    else:
        status = "success"

    run.finished_at = _now()
    run.status = status
    run.retry_count = stats.retries
    run.records_received = stats.records_received
    run.records_ingested = ingested
    run.records_quarantined = quarantined
    run.error_message = error_message
    db.commit()

    duration = time.perf_counter() - started_perf
    logger.info(
        "source=%s status=%s received=%s ingested=%s quarantined=%s "
        "retries=%s duration=%.2fs",
        spec.name,
        status,
        stats.records_received,
        ingested,
        quarantined,
        stats.retries,
        duration,
    )

    return {
        "source": spec.name,
        "ingestion_run_id": run_id,
        "status": status,
        "received": stats.records_received,
        "ingested": ingested,
        "quarantined": quarantined,
        "rows_inserted": load.inserted,
        "rows_updated": load.updated,
        "duplicates_dropped": load.deduplicated,
        "retries": stats.retries,
        "pages_fetched": stats.pages_fetched,
        "duration_seconds": round(duration, 3),
        "error_message": error_message,
    }


def run_ingestion(
    db: Session,
    world_seed: int = 42,
    failure_config: FailureConfig | None = None,
    sources: Sequence[str] | None = None,
    *,
    world_config: WorldConfig | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Ingest every source in order and return a per-source summary.

    `world_config` is optional and overrides `world_seed` — the CLI uses it to
    pass `--users` through to the generator. `sleep_fn` is injectable so tests
    do not actually sleep through backoff.
    """
    config = world_config or WorldConfig(seed=world_seed)
    failure_config = failure_config if failure_config is not None else FailureConfig()
    selected = tuple(sources) if sources else SOURCE_ORDER

    unknown = [name for name in selected if name not in SOURCE_ORDER]
    if unknown:
        raise ValueError(f"unknown sources: {unknown}")

    # Preserve the canonical order regardless of how they were requested.
    ordered = [name for name in SOURCE_ORDER if name in selected]

    world = build_world(config)
    specs = build_source_specs(world, failure_config, config.seed)

    logger.info(
        "starting ingestion seed=%s users=%s sources=%s",
        config.seed,
        config.n_users,
        ",".join(ordered),
    )

    results = [_ingest_source(db, specs[name], sleep_fn=sleep_fn) for name in ordered]

    totals = {
        key: sum(result[key] for result in results)
        for key in (
            "received",
            "ingested",
            "quarantined",
            "rows_inserted",
            "rows_updated",
            "duplicates_dropped",
            "retries",
        )
    }
    totals["duration_seconds"] = round(
        sum(result["duration_seconds"] for result in results), 3
    )

    return {
        "seed": config.seed,
        "world_summary": world.summary(),
        "sources": results,
        "totals": totals,
        "ingestion_run_ids": [result["ingestion_run_id"] for result in results],
    }
