"""Read-only rollups over the attribution results.

Plain functions returning plain dicts and lists — no FastAPI, no response
models. Phase 6 will wrap these; keeping them framework-free means they are
testable and reusable from the CLI.

Aggregation happens in SQL wherever practical. Money stays `Decimal` all the
way out; serialising it is the caller's problem.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AdSpend,
    AttributionResult,
    Campaign,
    Conversion,
    IngestionRun,
    QuarantinedRecord,
    Touchpoint,
)
from app.attribution.models import MODEL_NAMES

UTC = dt.timezone.utc

ZERO_MONEY = Decimal("0.00")

# date_trunc takes a literal, so the allowed values are whitelisted rather
# than interpolated from user input.
GRANULARITIES = {"day": "day", "week": "week", "month": "month"}

MIN_INTERESTING_TOUCHPOINTS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conversion_window(
    start_date: dt.date | None, end_date: dt.date | None
) -> list[Any]:
    """Filters on `conversions.occurred_at` for an inclusive date range.

    Explicit UTC datetime bounds rather than casting to date, so the result
    does not depend on the database session's timezone.
    """
    filters: list[Any] = []
    if start_date is not None:
        filters.append(
            Conversion.occurred_at
            >= dt.datetime.combine(start_date, dt.time.min, tzinfo=UTC)
        )
    if end_date is not None:
        filters.append(
            Conversion.occurred_at
            <= dt.datetime.combine(end_date, dt.time.max, tzinfo=UTC)
        )
    return filters


def _spend_window(start_date: dt.date | None, end_date: dt.date | None) -> list[Any]:
    filters: list[Any] = []
    if start_date is not None:
        filters.append(AdSpend.date >= start_date)
    if end_date is not None:
        filters.append(AdSpend.date <= end_date)
    return filters


def _spend_by_channel(
    db: Session, start_date: dt.date | None, end_date: dt.date | None
) -> dict[str, Decimal]:
    """Total spend per channel. Kept separate from the attribution aggregate
    so that joining spend to touchpoints cannot multiply it."""
    rows = db.execute(
        select(Campaign.channel, func.coalesce(func.sum(AdSpend.spend), 0))
        .join(Campaign, Campaign.id == AdSpend.campaign_id)
        .where(*_spend_window(start_date, end_date))
        .group_by(Campaign.channel)
    ).all()
    return {channel: Decimal(total) for channel, total in rows}


def _roas(revenue: Decimal, spend: Decimal) -> Decimal | None:
    """Return on ad spend, or None when there was no spend.

    Organic has zero spend by definition, so this must never divide.
    """
    if spend is None or spend == 0:
        return None
    return (revenue / spend).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# 1. Channel performance
# ---------------------------------------------------------------------------


def channel_performance(
    db: Session,
    *,
    model_name: str,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Per-channel attributed revenue, conversions, spend and ROAS.

    `attributed_conversions` is the sum of credit, so it is fractional: a
    conversion touched by three channels contributes a fraction to each
    rather than being counted three times.
    """
    attribution_rows = db.execute(
        select(
            Touchpoint.channel,
            func.coalesce(func.sum(AttributionResult.attributed_revenue), 0).label(
                "attributed_revenue"
            ),
            func.coalesce(func.sum(AttributionResult.credit), 0).label(
                "attributed_conversions"
            ),
            func.count().label("touchpoint_count"),
        )
        .join(Touchpoint, Touchpoint.id == AttributionResult.touchpoint_id)
        .join(Conversion, Conversion.id == AttributionResult.conversion_id)
        .where(AttributionResult.model_name == model_name, *_conversion_window(start_date, end_date))
        .group_by(Touchpoint.channel)
    ).all()

    spend_by_channel = _spend_by_channel(db, start_date, end_date)

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for channel, revenue, conversions, touchpoint_count in attribution_rows:
        seen.add(channel)
        spend = spend_by_channel.get(channel, ZERO_MONEY)
        results.append(
            {
                "channel": channel,
                "attributed_revenue": Decimal(revenue),
                "attributed_conversions": Decimal(conversions),
                "spend": spend,
                "roas": _roas(Decimal(revenue), spend),
                "touchpoint_count": touchpoint_count,
            }
        )

    # Channels that cost money but earned no credit still belong in the table —
    # they are exactly the ones worth questioning.
    for channel, spend in spend_by_channel.items():
        if channel in seen:
            continue
        results.append(
            {
                "channel": channel,
                "attributed_revenue": ZERO_MONEY,
                "attributed_conversions": Decimal(0),
                "spend": spend,
                "roas": _roas(ZERO_MONEY, spend),
                "touchpoint_count": 0,
            }
        )

    results.sort(key=lambda row: row["attributed_revenue"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# 2. Model comparison
# ---------------------------------------------------------------------------


def model_comparison(
    db: Session,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    models: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Attributed revenue per channel under every model, plus the swing.

    `swing` is max minus min across models: the amount of revenue whose owner
    is disputed. A large swing means the channel's apparent value depends
    mostly on which model you believe.
    """
    model_names = tuple(models) if models else MODEL_NAMES

    rows = db.execute(
        select(
            Touchpoint.channel,
            AttributionResult.model_name,
            func.coalesce(func.sum(AttributionResult.attributed_revenue), 0),
        )
        .join(Touchpoint, Touchpoint.id == AttributionResult.touchpoint_id)
        .join(Conversion, Conversion.id == AttributionResult.conversion_id)
        .where(
            AttributionResult.model_name.in_(list(model_names)),
            *_conversion_window(start_date, end_date),
        )
        .group_by(Touchpoint.channel, AttributionResult.model_name)
    ).all()

    by_channel: dict[str, dict[str, Decimal]] = {}
    for channel, model_name, revenue in rows:
        by_channel.setdefault(channel, {})[model_name] = Decimal(revenue)

    comparison: list[dict[str, Any]] = []
    for channel, per_model in by_channel.items():
        values = {name: per_model.get(name, ZERO_MONEY) for name in model_names}
        largest = max(values.values())
        smallest = min(values.values())
        winner = max(values, key=lambda name: values[name])
        loser = min(values, key=lambda name: values[name])
        comparison.append(
            {
                "channel": channel,
                "by_model": values,
                "max_revenue": largest,
                "min_revenue": smallest,
                "swing": largest - smallest,
                "swing_pct": (
                    ((largest - smallest) / largest * 100).quantize(Decimal("0.1"))
                    if largest > 0
                    else Decimal("0.0")
                ),
                "most_generous_model": winner,
                "least_generous_model": loser,
            }
        )

    comparison.sort(key=lambda row: row["swing"], reverse=True)

    totals = {
        name: sum((row["by_model"][name] for row in comparison), ZERO_MONEY)
        for name in model_names
    }
    return {
        "models": list(model_names),
        "channels": comparison,
        "totals_by_model": totals,
        "total_swing": sum((row["swing"] for row in comparison), ZERO_MONEY),
    }


# ---------------------------------------------------------------------------
# 3. Timeseries
# ---------------------------------------------------------------------------


def timeseries(
    db: Session,
    *,
    model_name: str,
    granularity: str = "day",
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Attributed revenue per channel per period (day by default)."""
    if granularity not in GRANULARITIES:
        raise ValueError(
            f"unknown granularity {granularity!r} (expected one of {sorted(GRANULARITIES)})"
        )

    bucket = func.date_trunc(GRANULARITIES[granularity], Conversion.occurred_at).label(
        "bucket"
    )
    rows = db.execute(
        select(
            bucket,
            Touchpoint.channel,
            func.coalesce(func.sum(AttributionResult.attributed_revenue), 0),
            func.coalesce(func.sum(AttributionResult.credit), 0),
        )
        .join(Touchpoint, Touchpoint.id == AttributionResult.touchpoint_id)
        .join(Conversion, Conversion.id == AttributionResult.conversion_id)
        .where(AttributionResult.model_name == model_name, *_conversion_window(start_date, end_date))
        .group_by(bucket, Touchpoint.channel)
        .order_by(bucket, Touchpoint.channel)
    ).all()

    return [
        {
            "period": period.date() if isinstance(period, dt.datetime) else period,
            "channel": channel,
            "attributed_revenue": Decimal(revenue),
            "attributed_conversions": Decimal(conversions),
        }
        for period, channel, revenue, conversions in rows
    ]


# ---------------------------------------------------------------------------
# 4. Top journeys
# ---------------------------------------------------------------------------


def top_journeys(
    db: Session, *, model_name: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Sample conversions with their full path and per-touch credit.

    Journeys with 3+ touchpoints are preferred — a single-touch journey has
    nothing to say about multi-touch attribution — but shorter ones are
    returned rather than an empty list if that is all there is.
    """
    touchpoint_count = func.count().label("touchpoint_count")
    candidates = db.execute(
        select(
            AttributionResult.conversion_id,
            touchpoint_count,
            func.sum(AttributionResult.attributed_revenue).label("revenue"),
        )
        .where(AttributionResult.model_name == model_name)
        .group_by(AttributionResult.conversion_id)
        .order_by(
            # Prefer the interesting ones, then the biggest.
            case((touchpoint_count >= MIN_INTERESTING_TOUCHPOINTS, 1), else_=0).desc(),
            func.sum(AttributionResult.attributed_revenue).desc(),
        )
        .limit(limit)
    ).all()

    conversion_ids = [row.conversion_id for row in candidates]
    if not conversion_ids:
        return []

    # One query for every touchpoint of every selected journey.
    detail_rows = db.execute(
        select(
            AttributionResult.conversion_id,
            Conversion.external_id,
            Conversion.user_id,
            Conversion.occurred_at,
            Conversion.revenue,
            Touchpoint.id,
            Touchpoint.channel,
            Touchpoint.touch_type,
            Touchpoint.occurred_at,
            AttributionResult.credit,
            AttributionResult.attributed_revenue,
        )
        .join(Conversion, Conversion.id == AttributionResult.conversion_id)
        .join(Touchpoint, Touchpoint.id == AttributionResult.touchpoint_id)
        .where(
            AttributionResult.model_name == model_name,
            AttributionResult.conversion_id.in_(conversion_ids),
        )
        .order_by(AttributionResult.conversion_id, Touchpoint.occurred_at, Touchpoint.id)
    ).all()

    journeys: dict[int, dict[str, Any]] = {}
    for row in detail_rows:
        journey = journeys.setdefault(
            row[0],
            {
                "conversion_id": row[0],
                "conversion_external_id": row[1],
                "user_id": row[2],
                "converted_at": row[3],
                "revenue": Decimal(row[4]),
                "touchpoints": [],
            },
        )
        journey["touchpoints"].append(
            {
                "touchpoint_id": row[5],
                "channel": row[6],
                "touch_type": row[7],
                "occurred_at": row[8],
                "credit": Decimal(row[9]),
                "attributed_revenue": Decimal(row[10]),
            }
        )

    # Preserve the ranking from the candidate query.
    ordered = [journeys[cid] for cid in conversion_ids if cid in journeys]
    for journey in ordered:
        journey["touchpoint_count"] = len(journey["touchpoints"])
        journey["path"] = " -> ".join(
            touch["channel"] for touch in journey["touchpoints"]
        )
    return ordered


# ---------------------------------------------------------------------------
# 5. Pipeline health
# ---------------------------------------------------------------------------


def pipeline_health(db: Session, *, recent_runs: int = 20) -> dict[str, Any]:
    """Ingestion runs, quarantine breakdown and row counts.

    Surfaces the pipeline's own work so the dashboard can show data quality
    rather than pretending the numbers arrived perfectly.
    """
    runs = (
        db.execute(
            select(IngestionRun).order_by(IngestionRun.id.desc()).limit(recent_runs)
        )
        .scalars()
        .all()
    )

    quarantine_rows = db.execute(
        select(
            QuarantinedRecord.source,
            QuarantinedRecord.error_reason,
            func.count().label("count"),
        )
        .group_by(QuarantinedRecord.source, QuarantinedRecord.error_reason)
        .order_by(func.count().desc())
    ).all()

    by_source: dict[str, int] = {}
    for source, _reason, count in quarantine_rows:
        by_source[source] = by_source.get(source, 0) + count

    counts = db.execute(
        select(
            select(func.count()).select_from(Campaign).scalar_subquery(),
            select(func.count()).select_from(AdSpend).scalar_subquery(),
            select(func.count()).select_from(Touchpoint).scalar_subquery(),
            select(func.count()).select_from(Conversion).scalar_subquery(),
            select(func.count()).select_from(AttributionResult).scalar_subquery(),
            select(func.count()).select_from(QuarantinedRecord).scalar_subquery(),
            select(func.count()).select_from(IngestionRun).scalar_subquery(),
        )
    ).one()

    return {
        "recent_runs": [
            {
                "id": run.id,
                "source": run.source,
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "records_received": run.records_received,
                "records_ingested": run.records_ingested,
                "records_quarantined": run.records_quarantined,
                "retry_count": run.retry_count,
                "error_message": run.error_message,
            }
            for run in runs
        ],
        "quarantine": {
            "total": sum(by_source.values()),
            "by_source": by_source,
            "by_reason": [
                {"source": source, "error_reason": reason, "count": count}
                for source, reason, count in quarantine_rows
            ],
        },
        "row_counts": {
            "campaigns": counts[0],
            "ad_spend": counts[1],
            "touchpoints": counts[2],
            "conversions": counts[3],
            "attribution_results": counts[4],
            "quarantined_records": counts[5],
            "ingestion_runs": counts[6],
        },
    }
