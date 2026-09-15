"""Idempotent batch upserts.

Everything here is written so that ingesting the same data twice is a no-op
beyond timestamps: each loader upserts on the natural key the Phase 2 schema
already enforces, in batches, with one campaign-id lookup per batch rather
than per row.

Counting inserted vs updated uses Postgres's `xmax` system column: it is 0 for
a row this statement inserted and non-zero for one it updated. That keeps the
whole thing to a single round trip per batch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import AdSpend, Campaign, Conversion, Touchpoint
from app.pipeline.schemas import AdSpendIn, CampaignIn, ConversionIn, TouchpointIn

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

# True for rows this statement inserted, false for rows it updated.
_WAS_INSERTED = literal_column("xmax = 0").label("inserted")


@dataclass
class LoadResult:
    """Outcome of loading one source's records."""

    inserted: int = 0
    updated: int = 0
    # Dropped before the upsert because an earlier record in the same batch
    # had the same conflict key. Postgres cannot touch one key twice in a
    # single ON CONFLICT statement.
    deduplicated: int = 0
    # (validated_model, reason) pairs for records the database would reject on
    # referential grounds. The caller quarantines these.
    rejected: list[tuple[Any, str]] = field(default_factory=list)

    @property
    def affected(self) -> int:
        return self.inserted + self.updated

    def merge(self, other: "LoadResult") -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.deduplicated += other.deduplicated
        self.rejected.extend(other.rejected)


def _chunks(items: Sequence[Any], size: int = BATCH_SIZE) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _dedupe(records: Iterable[Any], key_fn) -> tuple[list[Any], int]:
    """Keep the last record per conflict key; report how many were dropped.

    Last-wins matches upsert semantics: if the upstream sent the same key
    twice in one page, the later value is the one an ON CONFLICT DO UPDATE
    would have left behind anyway.
    """
    by_key: dict[Any, Any] = {}
    dropped = 0
    for record in records:
        key = key_fn(record)
        if key in by_key:
            dropped += 1
        by_key[key] = record
    return list(by_key.values()), dropped


def _resolve_campaign_ids(db: Session, external_ids: Iterable[str]) -> dict[str, int]:
    """One query per batch: campaign external_id -> database id."""
    wanted = {external_id for external_id in external_ids if external_id}
    if not wanted:
        return {}
    rows = db.execute(
        select(Campaign.external_id, Campaign.id).where(
            Campaign.external_id.in_(wanted)
        )
    ).all()
    return {external_id: campaign_id for external_id, campaign_id in rows}


def _tally(db: Session, stmt) -> tuple[int, int]:
    """Run an upsert and split the returned rows into inserted / updated."""
    rows = db.execute(stmt).all()
    inserted = sum(1 for row in rows if row.inserted)
    return inserted, len(rows) - inserted


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def upsert_campaigns(db: Session, records: Sequence[CampaignIn]) -> LoadResult:
    """Upsert campaigns, conflicting on external_id."""
    result = LoadResult()
    if not records:
        return result

    unique, result.deduplicated = _dedupe(records, lambda r: r.external_id)

    for batch in _chunks(unique):
        values = [
            {
                "external_id": record.external_id,
                "name": record.name,
                "channel": record.channel,
                "platform": record.platform,
            }
            for record in batch
        ]
        stmt = pg_insert(Campaign).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Campaign.external_id],
            set_={
                "name": stmt.excluded.name,
                "channel": stmt.excluded.channel,
                "platform": stmt.excluded.platform,
            },
        ).returning(Campaign.id, _WAS_INSERTED)

        inserted, updated = _tally(db, stmt)
        result.inserted += inserted
        result.updated += updated

    return result


# ---------------------------------------------------------------------------
# Ad spend
# ---------------------------------------------------------------------------


def upsert_ad_spend(db: Session, records: Sequence[AdSpendIn]) -> LoadResult:
    """Upsert daily spend, conflicting on (campaign_id, date).

    A row naming a campaign we have never seen is rejected rather than
    dropped: `ad_spend.campaign_id` is NOT NULL, so there is no way to store
    it, and silently discarding spend would quietly understate cost.
    """
    result = LoadResult()
    if not records:
        return result

    unique, result.deduplicated = _dedupe(
        records, lambda r: (r.campaign_external_id, r.date)
    )

    for batch in _chunks(unique):
        campaign_ids = _resolve_campaign_ids(
            db, (record.campaign_external_id for record in batch)
        )

        values = []
        for record in batch:
            campaign_id = campaign_ids.get(record.campaign_external_id)
            if campaign_id is None:
                result.rejected.append(
                    (
                        record,
                        f"unknown campaign_id {record.campaign_external_id!r} "
                        "(no matching campaign)",
                    )
                )
                continue
            values.append(
                {
                    "campaign_id": campaign_id,
                    "date": record.date,
                    "spend": record.spend,
                    "impressions": record.impressions,
                    "clicks": record.clicks,
                }
            )

        if not values:
            continue

        stmt = pg_insert(AdSpend).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AdSpend.campaign_id, AdSpend.date],
            set_={
                "spend": stmt.excluded.spend,
                "impressions": stmt.excluded.impressions,
                "clicks": stmt.excluded.clicks,
                "ingested_at": stmt.excluded.ingested_at,
            },
        ).returning(AdSpend.id, _WAS_INSERTED)

        inserted, updated = _tally(db, stmt)
        result.inserted += inserted
        result.updated += updated

    return result


# ---------------------------------------------------------------------------
# Touchpoints
# ---------------------------------------------------------------------------


def upsert_touchpoints(db: Session, records: Sequence[TouchpointIn]) -> LoadResult:
    """Upsert touchpoints, conflicting on external_id.

    A null campaign_external_id is legitimate (organic traffic). A campaign
    that does not exist is a referential error and is rejected for
    quarantine — NOT nulled, because nulling it would silently relabel paid
    traffic as organic and corrupt every attribution result downstream.
    """
    result = LoadResult()
    if not records:
        return result

    unique, result.deduplicated = _dedupe(records, lambda r: r.external_id)

    for batch in _chunks(unique):
        campaign_ids = _resolve_campaign_ids(
            db,
            (
                record.campaign_external_id
                for record in batch
                if record.campaign_external_id is not None
            ),
        )

        values = []
        for record in batch:
            if record.campaign_external_id is None:
                campaign_id = None
            else:
                campaign_id = campaign_ids.get(record.campaign_external_id)
                if campaign_id is None:
                    result.rejected.append(
                        (
                            record,
                            f"unknown campaign_id {record.campaign_external_id!r} "
                            "(no matching campaign; refusing to null it)",
                        )
                    )
                    continue

            values.append(
                {
                    "external_id": record.external_id,
                    "user_id": record.user_id,
                    "campaign_id": campaign_id,
                    "channel": record.channel,
                    "occurred_at": record.occurred_at,
                    "touch_type": record.touch_type,
                }
            )

        if not values:
            continue

        stmt = pg_insert(Touchpoint).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Touchpoint.external_id],
            set_={
                "user_id": stmt.excluded.user_id,
                "campaign_id": stmt.excluded.campaign_id,
                "channel": stmt.excluded.channel,
                "occurred_at": stmt.excluded.occurred_at,
                "touch_type": stmt.excluded.touch_type,
                "ingested_at": stmt.excluded.ingested_at,
            },
        ).returning(Touchpoint.id, _WAS_INSERTED)

        inserted, updated = _tally(db, stmt)
        result.inserted += inserted
        result.updated += updated

    return result


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def upsert_conversions(db: Session, records: Sequence[ConversionIn]) -> LoadResult:
    """Upsert conversions, conflicting on external_id."""
    result = LoadResult()
    if not records:
        return result

    unique, result.deduplicated = _dedupe(records, lambda r: r.external_id)

    for batch in _chunks(unique):
        values = [
            {
                "external_id": record.external_id,
                "user_id": record.user_id,
                "occurred_at": record.occurred_at,
                "revenue": record.revenue,
                "currency": record.currency,
            }
            for record in batch
        ]
        stmt = pg_insert(Conversion).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Conversion.external_id],
            set_={
                "user_id": stmt.excluded.user_id,
                "occurred_at": stmt.excluded.occurred_at,
                "revenue": stmt.excluded.revenue,
                "currency": stmt.excluded.currency,
                "ingested_at": stmt.excluded.ingested_at,
            },
        ).returning(Conversion.id, _WAS_INSERTED)

        inserted, updated = _tally(db, stmt)
        result.inserted += inserted
        result.updated += updated

    return result
