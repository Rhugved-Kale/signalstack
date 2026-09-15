"""Fake HTTP-like APIs over a synthetic world.

These classes exist to make the ingestion layer earn its keep. They serve the
coherent world from `world.py`, but they behave like real third-party APIs:
they rate-limit, they 500, they time out, they hand back a string where a
number belongs, and they occasionally repeat a record you have already seen.

Determinism: every failure and every corruption is drawn from one seeded
`random.Random` per API instance, so a given seed replays the exact same
sequence of disasters. `FailureConfig.none()` turns all of it off.

No database, no ORM, no `app.db` import — these return plain dicts.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from app.generator.world import World

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FakeAPIError(Exception):
    """Base class for every simulated API failure.

    `status_code` is `None` for failures that never produced an HTTP response
    at all (see `FakeTimeoutError`).
    """

    status_code: int | None = None

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class RateLimitError(FakeAPIError):
    """HTTP 429. Carries `retry_after` seconds, like a real Retry-After header."""

    status_code = 429

    def __init__(self, message: str = "Too Many Requests", retry_after: int = 1) -> None:
        super().__init__(message, 429)
        self.retry_after = retry_after


class ServerError(FakeAPIError):
    """HTTP 500 or 503 — the upstream broke, retrying may well work."""

    def __init__(self, message: str = "Internal Server Error", status_code: int = 500) -> None:
        super().__init__(message, status_code)


class FakeTimeoutError(FakeAPIError):
    """The request never came back.

    `status_code` stays `None` on purpose: a client-side timeout means no
    response was received, so there is no HTTP status to report.
    """

    status_code = None

    def __init__(self, message: str = "Request timed out", timeout_seconds: float = 30.0) -> None:
        super().__init__(message, None)
        self.status_code = None
        self.timeout_seconds = timeout_seconds


# ---------------------------------------------------------------------------
# Failure configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureConfig:
    """How often each kind of misbehaviour happens. All rates are 0.0-1.0."""

    server_error_rate: float = 0.08
    rate_limit_rate: float = 0.05
    timeout_rate: float = 0.03
    malformed_record_rate: float = 0.07
    duplicate_record_rate: float = 0.02

    def __post_init__(self) -> None:
        for name in (
            "server_error_rate",
            "rate_limit_rate",
            "timeout_rate",
            "malformed_record_rate",
            "duplicate_record_rate",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0.0 and 1.0, got {value!r}")

    @classmethod
    def none(cls) -> "FailureConfig":
        """A perfectly behaved API. Use this in deterministic tests."""
        return cls(
            server_error_rate=0.0,
            rate_limit_rate=0.0,
            timeout_rate=0.0,
            malformed_record_rate=0.0,
            duplicate_record_rate=0.0,
        )


# ---------------------------------------------------------------------------
# Record shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordShape:
    """Describes one endpoint's payload so corruptors know what to attack."""

    required: tuple[str, ...]
    numeric: tuple[str, ...] = ()
    money: tuple[str, ...] = ()
    timestamps: tuple[str, ...] = ()
    ids: tuple[str, ...] = ()
    currency: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Malformed-record variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Corruptor:
    """One way to break a record.

    `requires` names the `RecordShape` field that must be non-empty for this
    corruptor to apply, or `None` if it always applies.
    """

    name: str
    requires: str | None
    apply: Callable[[dict[str, Any], RecordShape, random.Random], None]


CORRUPTORS: list[Corruptor] = []


def corruptor(name: str, requires: str | None) -> Callable[..., Any]:
    """Register a malformed-record variant. Add new ones by writing a function."""

    def decorator(fn: Callable[[dict[str, Any], RecordShape, random.Random], None]):
        CORRUPTORS.append(Corruptor(name=name, requires=requires, apply=fn))
        return fn

    return decorator


@corruptor("missing_required_key", "required")
def _missing_required_key(record, shape, rng) -> None:
    record.pop(rng.choice(list(shape.required)), None)


@corruptor("numeric_as_string", "numeric")
def _numeric_as_string(record, shape, rng) -> None:
    key = rng.choice(list(shape.numeric))
    value = record.get(key)
    record[key] = f"{value:.2f}" if isinstance(value, float) else str(value)


@corruptor("numeric_null", "numeric")
def _numeric_null(record, shape, rng) -> None:
    record[rng.choice(list(shape.numeric))] = None


@corruptor("timestamp_garbage", "timestamps")
def _timestamp_garbage(record, shape, rng) -> None:
    record[rng.choice(list(shape.timestamps))] = rng.choice(
        ("not-a-date", "", "0000-00-00", "yesterday")
    )


@corruptor("timestamp_unix_int", "timestamps")
def _timestamp_unix_int(record, shape, rng) -> None:
    # An upstream that "helpfully" switched to epoch seconds mid-stream.
    record[rng.choice(list(shape.timestamps))] = rng.randint(1_600_000_000, 1_800_000_000)


@corruptor("negative_money", "money")
def _negative_money(record, shape, rng) -> None:
    key = rng.choice(list(shape.money))
    value = record.get(key)
    record[key] = -abs(value) if isinstance(value, (int, float)) else -1.0


@corruptor("unexpected_currency", "currency")
def _unexpected_currency(record, shape, rng) -> None:
    record[rng.choice(list(shape.currency))] = rng.choice(("XYZ", "usd", "", "US$", "999"))


@corruptor("empty_id", "ids")
def _empty_id(record, shape, rng) -> None:
    # Note: an empty string is NOT the same as a legitimately null campaign_id
    # on an organic touchpoint. Ingestion has to tell those apart.
    record[rng.choice(list(shape.ids))] = ""


@corruptor("extra_unexpected_field", None)
def _extra_unexpected_field(record, shape, rng) -> None:
    # Upstreams add fields without warning. This must never be fatal.
    key, value = rng.choice(
        (
            ("_debug_trace_id", "trace-" + str(rng.randint(10**6, 10**7))),
            ("experiment_bucket", rng.choice(("control", "variant_a"))),
            ("beta_metric", rng.random()),
            ("__v", 2),
        )
    )
    record[key] = value


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _iso_z(value: dt.datetime) -> str:
    """ISO 8601 in UTC with a trailing Z, the way most event APIs render it."""
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_date(value: dt.date | dt.datetime | None) -> dt.date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, dt.datetime) else value


# ---------------------------------------------------------------------------
# Base API
# ---------------------------------------------------------------------------


class _FakeAPI:
    """Shared failure rolling, pagination and corruption logic."""

    def __init__(
        self,
        world: World,
        failure_config: FailureConfig | None = None,
        seed: int = 0,
    ) -> None:
        self.world = world
        self.failures = failure_config or FailureConfig()
        self.seed = seed
        self._rng = random.Random(seed)
        # Clean copies of everything already handed out, for replaying dupes.
        self._emitted: list[dict[str, Any]] = []

    # -- failures ---------------------------------------------------------

    def _roll_failure(self) -> None:
        """Roll for transport failures before doing any work.

        Always consumes exactly three draws so the RNG stream stays aligned
        regardless of which failure (if any) fires.
        """
        server_roll = self._rng.random()
        rate_roll = self._rng.random()
        timeout_roll = self._rng.random()

        if server_roll < self.failures.server_error_rate:
            status = self._rng.choice((500, 503))
            raise ServerError(
                "Service Unavailable" if status == 503 else "Internal Server Error",
                status_code=status,
            )
        if rate_roll < self.failures.rate_limit_rate:
            raise RateLimitError(retry_after=self._rng.choice((1, 2, 5, 10)))
        if timeout_roll < self.failures.timeout_rate:
            raise FakeTimeoutError()

    # -- corruption -------------------------------------------------------

    def _corrupt(self, record: dict[str, Any], shape: RecordShape) -> dict[str, Any]:
        applicable = [
            c
            for c in CORRUPTORS
            if c.requires is None or getattr(shape, c.requires)
        ]
        if not applicable:
            return record
        chosen = self._rng.choice(applicable)
        chosen.apply(record, shape, self._rng)
        return record

    # -- pagination -------------------------------------------------------

    def _respond(
        self,
        records: Sequence[dict[str, Any]],
        page: int,
        per_page: int,
        shape: RecordShape,
    ) -> dict[str, Any]:
        self._roll_failure()

        if page < 1 or per_page < 1:
            raise ValueError("page and per_page must both be >= 1")

        total = len(records)
        total_pages = max(1, math.ceil(total / per_page))
        start = (page - 1) * per_page
        window = records[start : start + per_page]

        data: list[dict[str, Any]] = []
        for record in window:
            emitted = dict(record)
            if self._rng.random() < self.failures.malformed_record_rate:
                emitted = self._corrupt(emitted, shape)
            data.append(emitted)

            # Remember the clean version so a later dupe looks like a real replay.
            self._emitted.append(dict(record))

            if self._emitted and self._rng.random() < self.failures.duplicate_record_rate:
                data.append(dict(self._rng.choice(self._emitted)))

        return {
            "data": data,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_more": page < total_pages,
        }


# ---------------------------------------------------------------------------
# 1. Ad platform
# ---------------------------------------------------------------------------


class FakeAdPlatformAPI(_FakeAPI):
    """Campaign metadata and daily spend, in ad-platform naming."""

    CAMPAIGN_SHAPE = RecordShape(
        required=("campaign_id", "campaign_name", "channel", "platform"),
        ids=("campaign_id",),
    )
    SPEND_SHAPE = RecordShape(
        required=("campaign_id", "date", "spend_usd", "impressions", "clicks"),
        numeric=("spend_usd", "impressions", "clicks"),
        money=("spend_usd",),
        timestamps=("date",),
        ids=("campaign_id",),
    )

    def list_campaigns(self, page: int = 1, per_page: int = 50) -> dict[str, Any]:
        records = [
            {
                "campaign_id": campaign["external_id"],
                "campaign_name": campaign["name"],
                "channel": campaign["channel"],
                "platform": campaign["platform"],
            }
            for campaign in self.world.campaigns
        ]
        return self._respond(records, page, per_page, self.CAMPAIGN_SHAPE)

    def list_ad_spend(
        self,
        start_date: dt.date | None = None,
        end_date: dt.date | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        start = _as_date(start_date)
        end = _as_date(end_date)
        records = [
            {
                "campaign_id": row["campaign_external_id"],
                "date": row["date"].isoformat(),
                "spend_usd": row["spend"],
                "impressions": row["impressions"],
                "clicks": row["clicks"],
            }
            for row in self.world.daily_spend
            if (start is None or row["date"] >= start)
            and (end is None or row["date"] <= end)
        ]
        return self._respond(records, page, per_page, self.SPEND_SHAPE)


# ---------------------------------------------------------------------------
# 2. Event stream
# ---------------------------------------------------------------------------


class FakeEventStreamAPI(_FakeAPI):
    """Per-user marketing events — the raw journey data."""

    TOUCHPOINT_SHAPE = RecordShape(
        # campaign_id is required to be *present*, but its value may be null
        # for organic traffic.
        required=("event_id", "user_id", "campaign_id", "channel", "event_type", "timestamp"),
        timestamps=("timestamp",),
        ids=("user_id", "campaign_id"),
    )

    def list_touchpoints(
        self,
        start_date: dt.date | None = None,
        end_date: dt.date | None = None,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        start = _as_date(start_date)
        end = _as_date(end_date)
        records = [
            {
                "event_id": touch["external_id"],
                "user_id": touch["user_id"],
                # None here is legitimate: organic traffic has no campaign.
                "campaign_id": touch["campaign_external_id"],
                "channel": touch["channel"],
                "event_type": touch["touch_type"],
                "timestamp": _iso_z(touch["occurred_at"]),
            }
            for touch in self.world.touchpoints
            if (start is None or touch["occurred_at"].date() >= start)
            and (end is None or touch["occurred_at"].date() <= end)
        ]
        return self._respond(records, page, per_page, self.TOUCHPOINT_SHAPE)


# ---------------------------------------------------------------------------
# 3. Payments
# ---------------------------------------------------------------------------


class FakePaymentAPI(_FakeAPI):
    """Orders from the payment processor — the revenue side."""

    CONVERSION_SHAPE = RecordShape(
        required=("order_id", "customer_id", "amount", "currency", "created_at"),
        numeric=("amount",),
        money=("amount",),
        timestamps=("created_at",),
        ids=("order_id", "customer_id"),
        currency=("currency",),
    )

    def list_conversions(
        self,
        start_date: dt.date | None = None,
        end_date: dt.date | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        start = _as_date(start_date)
        end = _as_date(end_date)
        records = [
            {
                "order_id": conversion["external_id"],
                "customer_id": conversion["user_id"],
                "amount": conversion["revenue"],
                "currency": conversion["currency"],
                "created_at": _iso_z(conversion["occurred_at"]),
            }
            for conversion in self.world.conversions
            if (start is None or conversion["occurred_at"].date() >= start)
            and (end is None or conversion["occurred_at"].date() <= end)
        ]
        return self._respond(records, page, per_page, self.CONVERSION_SHAPE)
