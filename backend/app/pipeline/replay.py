"""Dependency-ordered replay of quarantined records.

Phase 4 surfaced a cascade: one campaign record arrived with a dropped field,
so it was quarantined, and every child record referencing it was then
quarantined as an orphan. 56% of all quarantine volume traced back to one bad
parent row.

Replay fixes that from the top down. A quarantined campaign's stored payload is
corrupt, so re-validating it will fail forever — but the *entity* it describes
is still fetchable, and the upstream corrupts randomly per call. So we re-fetch
the campaigns source, upsert whatever came back intact, and only then re-drive
the children, whose payloads were never corrupt in the first place: they were
merely orphaned.

Everything here reuses the existing validation, loaders and fetcher. Nothing is
reimplemented.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Callable, Iterable, Sequence

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import Campaign, IngestionRun, QuarantinedRecord
from app.generator.fake_apis import FailureConfig
from app.generator.world import WorldConfig, build_world
from app.pipeline.fetcher import FetchStats, fetch_all_pages
from app.pipeline.loaders import upsert_campaigns
from app.pipeline.runner import SOURCE_ORDER, build_source_specs
from app.pipeline.schemas import describe_validation_error

logger = logging.getLogger(__name__)

# Parents before children, always. Same order the runner ingests in.
REPLAY_ORDER = SOURCE_ORDER

# Sources whose records reference a campaign; only these benefit from a parent
# re-fetch.
CAMPAIGN_DEPENDENT = ("campaigns", "ad_spend", "touchpoints")

# Offset the replay's RNG away from the original ingestion. Re-fetching with
# the ingestion's own seed would reproduce the identical corruption and recover
# nothing; a different seed per round gives the record a fresh chance while
# staying deterministic.
REPLAY_SEED_BASE = 1000
REPLAY_SEED_STRIDE = 17

DELETE_CHUNK = 1000


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _quarantine_counts(db: Session, sources: Sequence[str]) -> dict[str, int]:
    rows = db.execute(
        select(QuarantinedRecord.source, func.count())
        .where(QuarantinedRecord.source.in_(list(sources)))
        .group_by(QuarantinedRecord.source)
    ).all()
    counts = {source: 0 for source in sources}
    counts.update({source: count for source, count in rows})
    return counts


def _delete_quarantined(db: Session, row_ids: Sequence[int]) -> None:
    for start in range(0, len(row_ids), DELETE_CHUNK):
        chunk = row_ids[start : start + DELETE_CHUNK]
        db.execute(delete(QuarantinedRecord).where(QuarantinedRecord.id.in_(chunk)))


def _existing_campaign_ids(db: Session) -> set[str]:
    return set(db.execute(select(Campaign.external_id)).scalars().all())


def _refetch_missing_campaigns(
    db: Session,
    *,
    config: WorldConfig,
    failure_config: FailureConfig,
    seed: int,
    sleep_fn: Callable[[float], None] | None,
) -> tuple[int, int]:
    """Re-fetch campaigns and upsert any that are missing from the database.

    Returns (campaigns_recovered, retries). Only previously-missing campaigns
    are upserted, so the count means exactly "parents this repaired" and
    existing rows are left untouched.
    """
    world = build_world(config)
    spec = build_source_specs(world, failure_config, seed)["campaigns"]

    known = _existing_campaign_ids(db)
    stats = FetchStats()
    fetch_kwargs: dict[str, Any] = {"per_page": spec.per_page}
    if sleep_fn is not None:
        fetch_kwargs["sleep_fn"] = sleep_fn

    missing: list[Any] = []
    seen: set[str] = set()
    try:
        for raw in fetch_all_pages(
            spec.fetch_fn, source="replay:campaigns", stats=stats, **fetch_kwargs
        ):
            try:
                record = spec.model.model_validate(raw)
            except ValidationError:
                # Still corrupt on this call; a later round may get it clean.
                continue
            if record.external_id in known or record.external_id in seen:
                continue
            seen.add(record.external_id)
            missing.append(record)
    except Exception as exc:  # noqa: BLE001 - replay must not die on a bad fetch
        logger.warning("replay: campaign re-fetch failed: %s", exc)
        return 0, stats.retries

    if not missing:
        return 0, stats.retries

    result = upsert_campaigns(db, missing)
    logger.info(
        "replay: recovered %s previously-missing campaign(s) by re-fetch",
        result.affected,
    )
    return result.affected, stats.retries


def _replay_source(
    db: Session,
    source: str,
    *,
    config: WorldConfig,
    failure_config: FailureConfig,
    seed: int,
) -> int:
    """Re-validate and re-load one source's quarantined records.

    Returns how many rows were recovered (and therefore deleted from
    `quarantined_records`).
    """
    rows = (
        db.execute(
            select(QuarantinedRecord)
            .where(QuarantinedRecord.source == source)
            .order_by(QuarantinedRecord.id)
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    world = build_world(config)
    spec = build_source_specs(world, failure_config, seed)[source]

    # Campaigns are a special case: their stored payload is corrupt by
    # definition, but if the entity now exists (because the re-fetch repaired
    # it) the quarantine entry is obsolete.
    known_campaigns = _existing_campaign_ids(db) if source == "campaigns" else set()

    recovered_ids: list[int] = []
    valid: list[Any] = []
    row_by_model: dict[int, QuarantinedRecord] = {}

    for row in rows:
        payload = row.raw_payload or {}
        try:
            record = spec.model.model_validate(payload)
        except ValidationError as exc:
            reason = describe_validation_error(exc)
            if source == "campaigns":
                candidate = payload.get("campaign_id")
                if isinstance(candidate, str) and candidate in known_campaigns:
                    # Repaired by the parent re-fetch.
                    recovered_ids.append(row.id)
                    continue
            row.error_reason = reason
            continue
        valid.append(record)
        row_by_model[id(record)] = row

    if valid:
        result = spec.loader(db, valid)
        rejected = {id(record) for record, _ in result.rejected}
        for record, reason in result.rejected:
            row = row_by_model.get(id(record))
            if row is not None:
                row.error_reason = reason
        for model_id, row in row_by_model.items():
            # Anything the loader did not reject is now in the database —
            # including in-batch duplicates, whose twin carried the data.
            if model_id not in rejected:
                recovered_ids.append(row.id)

    if recovered_ids:
        _delete_quarantined(db, recovered_ids)

    return len(recovered_ids)


def replay_quarantine(
    db: Session,
    *,
    sources: Sequence[str] | None = None,
    max_rounds: int = 3,
    fetch_parents: bool = True,
    world_seed: int = 42,
    world_config: WorldConfig | None = None,
    failure_config: FailureConfig | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Replay quarantined records in dependency order.

    Parents are repaired before children, so an orphaned child gets a real
    chance instead of failing for the same reason twice. Recovered rows are
    deleted from `quarantined_records`; rows that fail again stay, with their
    `error_reason` refreshed to the current failure.

    `world_seed` / `world_config` / `failure_config` / `sleep_fn` mirror
    `run_ingestion` and are needed to drive the parent re-fetch.
    """
    config = world_config or WorldConfig(seed=world_seed)
    failure_config = failure_config if failure_config is not None else FailureConfig()

    selected = tuple(sources) if sources else REPLAY_ORDER
    unknown = [name for name in selected if name not in REPLAY_ORDER]
    if unknown:
        raise ValueError(f"unknown sources: {unknown}")
    ordered = [name for name in REPLAY_ORDER if name in selected]

    started_at = _now()
    started_perf = time.perf_counter()

    run = IngestionRun(source="replay", started_at=started_at, status="running")
    db.add(run)
    db.commit()

    before = _quarantine_counts(db, ordered)
    recovered = {source: 0 for source in ordered}
    parent_campaigns_recovered = 0
    retries = 0
    rounds_run = 0
    error_message: str | None = None

    wants_parents = fetch_parents and any(
        source in CAMPAIGN_DEPENDENT for source in ordered
    )

    try:
        for round_index in range(max_rounds):
            rounds_run += 1
            round_recovered = 0
            seed = config.seed + REPLAY_SEED_BASE + round_index * REPLAY_SEED_STRIDE

            if wants_parents:
                repaired, round_retries = _refetch_missing_campaigns(
                    db,
                    config=config,
                    failure_config=failure_config,
                    seed=seed,
                    sleep_fn=sleep_fn,
                )
                parent_campaigns_recovered += repaired
                retries += round_retries
                round_recovered += repaired

            for source in ordered:
                count = _replay_source(
                    db,
                    source,
                    config=config,
                    failure_config=failure_config,
                    seed=seed,
                )
                recovered[source] += count
                round_recovered += count

            db.commit()
            logger.info(
                "replay round %s/%s recovered %s item(s) "
                "(quarantine rows cleared + parent campaigns repaired)",
                round_index + 1,
                max_rounds,
                round_recovered,
            )
            if round_recovered == 0:
                break
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        error_message = f"{type(exc).__name__}: {exc}"
        logger.exception("replay failed")

    after = _quarantine_counts(db, ordered)
    total_attempted = sum(before.values())
    total_recovered = sum(recovered.values())
    total_remaining = sum(after.values())

    if error_message:
        status = "failed"
    elif total_remaining:
        status = "partial"
    else:
        status = "success"

    run.finished_at = _now()
    run.status = status
    run.retry_count = retries
    run.records_received = total_attempted
    run.records_ingested = total_recovered
    run.records_quarantined = total_remaining
    run.error_message = error_message
    db.commit()

    duration = time.perf_counter() - started_perf
    logger.info(
        "replay status=%s rounds=%s attempted=%s recovered=%s remaining=%s "
        "parents_recovered=%s duration=%.2fs",
        status,
        rounds_run,
        total_attempted,
        total_recovered,
        total_remaining,
        parent_campaigns_recovered,
        duration,
    )

    return {
        "replay_run_id": run.id,
        "status": status,
        "rounds": rounds_run,
        "parent_campaigns_recovered": parent_campaigns_recovered,
        "sources": {
            source: {
                "attempted": before[source],
                "recovered": recovered[source],
                "still_quarantined": after[source],
            }
            for source in ordered
        },
        "totals": {
            "attempted": total_attempted,
            "recovered": total_recovered,
            "still_quarantined": total_remaining,
            "retries": retries,
        },
        "duration_seconds": round(duration, 3),
        "error_message": error_message,
    }
