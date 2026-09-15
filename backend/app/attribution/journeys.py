"""Journey assembly: a conversion plus the touchpoints that preceded it.

The only interesting engineering here is not doing this one conversion at a
time. A naive implementation issues a query per conversion; this one walks
conversions in keyset-paginated batches and fetches every touchpoint for the
whole batch's users in a single query, so the query count is proportional to
the number of batches (2 per batch) rather than the number of conversions.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Conversion, Touchpoint

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_BATCH_SIZE = 500


@dataclass
class Journey:
    """One conversion and its ordered attribution window.

    `touchpoints` is ascending by `occurred_at`, ties broken by touchpoint id
    so the ordering is total and reproducible. It may legitimately be empty:
    a conversion with no tracked touches in the lookback window is a real
    outcome (direct/untracked), not an error.
    """

    conversion: Conversion
    touchpoints: list[Touchpoint] = field(default_factory=list)

    @property
    def revenue(self) -> Decimal:
        return self.conversion.revenue

    @property
    def user_id(self) -> str:
        return self.conversion.user_id

    @property
    def is_empty(self) -> bool:
        return not self.touchpoints

    def __len__(self) -> int:
        return len(self.touchpoints)


def load_journeys(
    db: Session,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Iterator[Journey]:
    """Yield a `Journey` per conversion, in batches of `batch_size`.

    A touchpoint belongs to a conversion's journey when it shares the user,
    happened at or before the conversion, and is within `lookback_days` of it.
    """
    lookback = dt.timedelta(days=lookback_days)
    queries = 0
    conversions_seen = 0
    empty_journeys = 0
    last_id = 0

    while True:
        batch = (
            db.execute(
                select(Conversion)
                .where(Conversion.id > last_id)
                .order_by(Conversion.id)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )
        queries += 1
        if not batch:
            break
        last_id = batch[-1].id

        # One touchpoint query for the whole batch. Bounding it by the batch's
        # own time range lets Postgres use the occurred_at index instead of
        # scanning every touchpoint the users ever had.
        user_ids = {conversion.user_id for conversion in batch}
        window_start = min(conversion.occurred_at for conversion in batch) - lookback
        window_end = max(conversion.occurred_at for conversion in batch)

        touchpoints = (
            db.execute(
                select(Touchpoint)
                .where(
                    Touchpoint.user_id.in_(user_ids),
                    Touchpoint.occurred_at >= window_start,
                    Touchpoint.occurred_at <= window_end,
                )
                .order_by(Touchpoint.occurred_at, Touchpoint.id)
            )
            .scalars()
            .all()
        )
        queries += 1

        # Group once, then slice per conversion. Already ordered by the query,
        # so each per-user list stays ordered.
        by_user: dict[str, list[Touchpoint]] = {}
        for touchpoint in touchpoints:
            by_user.setdefault(touchpoint.user_id, []).append(touchpoint)

        for conversion in batch:
            cutoff = conversion.occurred_at - lookback
            window = [
                touchpoint
                for touchpoint in by_user.get(conversion.user_id, ())
                if cutoff <= touchpoint.occurred_at <= conversion.occurred_at
            ]
            conversions_seen += 1
            if not window:
                empty_journeys += 1
            yield Journey(conversion=conversion, touchpoints=window)

        if len(batch) < batch_size:
            break

    logger.info(
        "loaded %s journeys (%s empty) in %s queries "
        "[lookback=%sd batch_size=%s]",
        conversions_seen,
        empty_journeys,
        queries,
        lookback_days,
        batch_size,
    )
