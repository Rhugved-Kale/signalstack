"""The scoring runner: journeys in, AttributionResult rows out.

One pass over the journeys computes every model, so the (comparatively
expensive) journey assembly is paid for once rather than once per model.
Writes are batched and upserted on
`(conversion_id, touchpoint_id, model_name)`, so re-running is idempotent.

Only non-zero credits are written. A zero-credit row would assert "this
touchpoint got nothing", which its absence already says, and it would make
`touchpoint_count` in the analytics layer mean something different for
`last_touch` than for `linear`. A row in this table means credit was assigned.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import delete, func, literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import AttributionResult
from app.attribution.journeys import DEFAULT_LOOKBACK_DAYS, load_journeys
from app.attribution.models import (
    DEFAULT_HALF_LIFE_DAYS,
    MODEL_NAMES,
    allocate_revenue,
    get_models,
)

logger = logging.getLogger(__name__)

WRITE_BATCH = 1000

_WAS_INSERTED = literal_column("xmax = 0").label("inserted")


def _blank_stats() -> dict[str, Any]:
    return {
        "conversions_scored": 0,
        "touchpoints_credited": 0,
        "rows_written": 0,
        "rows_inserted": 0,
        "rows_updated": 0,
        "conversions_with_empty_journey": 0,
        "total_attributed_revenue": Decimal("0.00"),
    }


def _flush(
    db: Session, rows: list[dict[str, Any]], stats: dict[str, dict[str, Any]]
) -> None:
    """Upsert one batch and record inserted/updated per model."""
    if not rows:
        return

    stmt = pg_insert(AttributionResult).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            AttributionResult.conversion_id,
            AttributionResult.touchpoint_id,
            AttributionResult.model_name,
        ],
        set_={
            "credit": stmt.excluded.credit,
            "attributed_revenue": stmt.excluded.attributed_revenue,
            "computed_at": func.now(),
        },
    ).returning(AttributionResult.model_name, _WAS_INSERTED)

    for model_name, inserted in db.execute(stmt).all():
        bucket = stats[model_name]
        if inserted:
            bucket["rows_inserted"] += 1
        else:
            bucket["rows_updated"] += 1

    rows.clear()


def run_attribution(
    db: Session,
    *,
    models: Sequence[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    half_life_days: float | int = DEFAULT_HALF_LIFE_DAYS,
    rebuild: bool = False,
) -> dict[str, Any]:
    """Score every journey under every requested model.

    With `rebuild=True`, existing rows for the selected models are deleted
    first — use it when a model's definition or the lookback changes, since a
    plain upsert cannot remove rows that should no longer exist.
    """
    registry = get_models(half_life_days)
    selected = tuple(models) if models else MODEL_NAMES

    unknown = [name for name in selected if name not in registry]
    if unknown:
        raise ValueError(f"unknown models: {unknown} (known: {sorted(registry)})")

    started = time.perf_counter()

    if rebuild:
        deleted = db.execute(
            delete(AttributionResult).where(
                AttributionResult.model_name.in_(list(selected))
            )
        ).rowcount
        db.commit()
        logger.info("rebuild: deleted %s existing row(s) for %s", deleted, ",".join(selected))

    stats = {name: _blank_stats() for name in selected}
    credited_touchpoints: dict[str, set[int]] = {name: set() for name in selected}
    rows: list[dict[str, Any]] = []
    journeys_seen = 0
    empty_journeys = 0

    for journey in load_journeys(db, lookback_days=lookback_days):
        journeys_seen += 1

        if journey.is_empty:
            # A real outcome, not an error: nothing tracked in the window.
            empty_journeys += 1
            for name in selected:
                stats[name]["conversions_with_empty_journey"] += 1
            continue

        revenue = journey.revenue
        for name in selected:
            credits = registry[name](journey, revenue)
            if not credits:
                continue
            revenues = allocate_revenue(credits, revenue)

            scored_any = False
            for (touchpoint, credit), attributed in zip(credits, revenues):
                if credit == 0:
                    continue
                scored_any = True
                rows.append(
                    {
                        "conversion_id": journey.conversion.id,
                        "touchpoint_id": touchpoint.id,
                        "model_name": name,
                        "credit": credit,
                        "attributed_revenue": attributed,
                    }
                )
                credited_touchpoints[name].add(touchpoint.id)
                stats[name]["rows_written"] += 1
                stats[name]["total_attributed_revenue"] += attributed

            if scored_any:
                stats[name]["conversions_scored"] += 1

        if len(rows) >= WRITE_BATCH:
            _flush(db, rows, stats)
            db.commit()

    _flush(db, rows, stats)
    db.commit()

    for name in selected:
        stats[name]["touchpoints_credited"] = len(credited_touchpoints[name])

    duration = time.perf_counter() - started
    for name in selected:
        bucket = stats[name]
        logger.info(
            "model=%s conversions_scored=%s touchpoints_credited=%s rows=%s "
            "(inserted=%s updated=%s) empty_journeys=%s revenue=%s",
            name,
            bucket["conversions_scored"],
            bucket["touchpoints_credited"],
            bucket["rows_written"],
            bucket["rows_inserted"],
            bucket["rows_updated"],
            bucket["conversions_with_empty_journey"],
            bucket["total_attributed_revenue"],
        )

    return {
        "models": stats,
        "journeys": journeys_seen,
        "empty_journeys": empty_journeys,
        "lookback_days": lookback_days,
        "half_life_days": half_life_days,
        "rebuild": rebuild,
        "duration_seconds": round(duration, 3),
        "totals": {
            "rows_written": sum(s["rows_written"] for s in stats.values()),
            "rows_inserted": sum(s["rows_inserted"] for s in stats.values()),
            "rows_updated": sum(s["rows_updated"] for s in stats.values()),
        },
    }
