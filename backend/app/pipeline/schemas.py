"""Pydantic v2 validation for incoming source records.

The generator's fake APIs hand back exactly the mess a real upstream does:
numbers as strings, nulls in required fields, epoch integers where an ISO
timestamp belongs, negative money, and fields nobody documented. This module
turns that into clean typed objects, or rejects it with a reason a human can
read in `quarantined_records.error_reason`.

Two principles:
  * Sloppiness is tolerated (a numeric string is still a number).
  * Corruption is rejected loudly — never silently coerced into a plausible
    looking value.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

UTC = dt.timezone.utc

# Money is stored as NUMERIC(12,2); quantize on the way in so the database
# never has to round silently behind us.
MONEY_EXPONENT = Decimal("0.01")

# Currencies this pipeline is prepared to handle. This is a *semantic* check,
# not a shape check: "XYZ" is a perfectly well-formed three-letter code and is
# still not money we can attribute revenue in. Widening the pipeline to a new
# currency is a deliberate act (FX handling, reporting), so it belongs in an
# explicit list rather than a regex.
SUPPORTED_CURRENCIES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "CAD",
        "AUD",
        "JPY",
        "CHF",
        "SEK",
        "NOK",
        "DKK",
        "NZD",
        "MXN",
    }
)


# ---------------------------------------------------------------------------
# Reusable coercions
# ---------------------------------------------------------------------------


def _require_non_empty_str(value: Any) -> Any:
    """Reject None, non-strings and blank/whitespace-only strings."""
    if value is None:
        raise ValueError("must not be null")
    if not isinstance(value, str):
        raise ValueError(f"must be a string, got {type(value).__name__}")
    if not value.strip():
        raise ValueError("must not be empty")
    return value.strip()


def _optional_non_empty_str(value: Any) -> Any:
    """Like `_require_non_empty_str`, but an explicit null is allowed.

    This is the organic-touchpoint case: `campaign_id: null` is legitimate,
    `campaign_id: ""` is corruption. The distinction matters, so an empty
    string is NOT quietly folded into None.
    """
    if value is None:
        return None
    return _require_non_empty_str(value)


def _parse_timestamp(value: Any) -> dt.datetime:
    """Parse ISO 8601 (with or without Z) or a Unix epoch int into UTC.

    Everything unparseable raises ValueError. `fromisoformat` raises
    ValueError on inputs like "0000-00-00" rather than returning something
    odd, and `fromtimestamp` can raise OverflowError/OSError on absurd
    epochs, so all of those are caught and re-raised as ValueError.
    """
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, bool):
        # bool is an int subclass; an epoch of True is nonsense.
        raise ValueError("must not be a boolean")
    elif isinstance(value, (int, float)):
        try:
            parsed = dt.datetime.fromtimestamp(value, tz=UTC)
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError(f"invalid unix timestamp {value!r}: {exc}") from exc
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            # Some upstreams send the epoch as a string.
            try:
                parsed = dt.datetime.fromtimestamp(int(text), tz=UTC)
            except (ValueError, OverflowError, OSError) as exc:
                raise ValueError(
                    f"unparseable timestamp {value!r} (expected ISO 8601 or unix epoch)"
                ) from exc
    else:
        raise ValueError(f"must be a timestamp, got {type(value).__name__}")

    # A naive timestamp is assumed to be UTC rather than rejected; the
    # alternative is throwing away otherwise good data.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_date(value: Any) -> dt.date:
    """Parse an ISO date, a datetime, or a Unix epoch into a `date`."""
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, bool):
        raise ValueError("must not be a boolean")
    if isinstance(value, (int, float)):
        return _parse_timestamp(value).date()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            # Fall back to the full timestamp parser (handles ISO datetimes
            # and epoch-as-string), which raises ValueError if it cannot cope.
            return _parse_timestamp(text).date()
    raise ValueError(f"must be a date, got {type(value).__name__}")


def _parse_money(value: Any) -> Decimal:
    """Coerce to Decimal without ever routing through binary float.

    A float that reached us from JSON is converted via `str()` so that 19.18
    stays 19.18 instead of becoming 19.179999999999999715782905696.
    """
    if value is None:
        raise ValueError("must not be null")
    if isinstance(value, bool):
        raise ValueError("must not be a boolean")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        amount = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"not a valid amount: {value!r}") from exc
    else:
        raise ValueError(f"must be a number, got {type(value).__name__}")

    if not amount.is_finite():
        raise ValueError(f"must be a finite amount, got {value!r}")
    return amount.quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def _parse_currency(value: Any) -> str:
    """A supported currency code, normalised to upper case.

    Uppercases first, so "usd" is accepted and stored as "USD", then checks
    membership in `SUPPORTED_CURRENCIES`. The rejection names the offending
    value so the reason stored alongside the raw payload is self-explanatory.
    """
    code = _require_non_empty_str(value).upper()
    if code not in SUPPORTED_CURRENCIES:
        raise ValueError(f"{value!r} is not a supported currency code")
    return code


NonEmptyStr = Annotated[str, BeforeValidator(_require_non_empty_str)]
OptionalStr = Annotated[str | None, BeforeValidator(_optional_non_empty_str)]
Timestamp = Annotated[dt.datetime, BeforeValidator(_parse_timestamp)]
CalendarDate = Annotated[dt.date, BeforeValidator(_parse_date)]
Money = Annotated[Decimal, BeforeValidator(_parse_money)]
CurrencyCode = Annotated[str, BeforeValidator(_parse_currency)]


def _non_negative_int(value: Any) -> int:
    """Strict-ish integer coercion that tolerates numeric strings."""
    if value is None:
        raise ValueError("must not be null")
    if isinstance(value, bool):
        raise ValueError("must not be a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"must be a whole number, got {value!r}")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        try:
            return int(text)
        except ValueError:
            # Accept "42.0" but not "42.50".
            try:
                as_decimal = Decimal(text)
            except InvalidOperation as exc:
                raise ValueError(f"not a valid integer: {value!r}") from exc
            if as_decimal != as_decimal.to_integral_value():
                raise ValueError(f"must be a whole number, got {value!r}")
            return int(as_decimal)
    raise ValueError(f"must be an integer, got {type(value).__name__}")


Count = Annotated[int, BeforeValidator(_non_negative_int), Field(ge=0)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class _SourceRecord(BaseModel):
    """Base config shared by every inbound record.

    `extra="ignore"` is the important one: upstreams add undocumented fields
    without warning and that must never be fatal.
    """

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )


class CampaignIn(_SourceRecord):
    """A campaign from the ad platform (`list_campaigns`)."""

    external_id: NonEmptyStr = Field(alias="campaign_id")
    name: NonEmptyStr = Field(alias="campaign_name")
    channel: NonEmptyStr
    platform: NonEmptyStr


class AdSpendIn(_SourceRecord):
    """One campaign-day of spend from the ad platform (`list_ad_spend`)."""

    campaign_external_id: NonEmptyStr = Field(alias="campaign_id")
    date: CalendarDate
    spend: Money = Field(alias="spend_usd", ge=0)
    impressions: Count
    clicks: Count

    @model_validator(mode="after")
    def _clicks_cannot_exceed_impressions(self) -> "AdSpendIn":
        if self.clicks > self.impressions:
            raise ValueError(
                f"clicks ({self.clicks}) cannot exceed impressions ({self.impressions})"
            )
        return self


class TouchpointIn(_SourceRecord):
    """A marketing event from the event stream (`list_touchpoints`)."""

    external_id: NonEmptyStr = Field(alias="event_id")
    user_id: NonEmptyStr
    # Required key, nullable value: organic traffic has no campaign, but a
    # MISSING key means the upstream dropped a field and we cannot tell.
    campaign_external_id: OptionalStr = Field(alias="campaign_id")
    channel: NonEmptyStr
    touch_type: NonEmptyStr = Field(alias="event_type")
    occurred_at: Timestamp = Field(alias="timestamp")


class ConversionIn(_SourceRecord):
    """An order from the payment API (`list_conversions`)."""

    external_id: NonEmptyStr = Field(alias="order_id")
    user_id: NonEmptyStr = Field(alias="customer_id")
    occurred_at: Timestamp = Field(alias="created_at")
    revenue: Money = Field(alias="amount", ge=0)
    currency: CurrencyCode


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------

MAX_REASON_LENGTH = 400


def describe_validation_error(exc: ValidationError, max_errors: int = 3) -> str:
    """Flatten a ValidationError into one short human-readable line.

    Field names come out as they appeared on the wire (the alias), so the
    stored reason matches the raw payload sitting next to it in
    `quarantined_records`.
    """
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "<record>"
        message = error["msg"]
        # Pydantic prefixes custom ValueErrors; drop the noise.
        if message.startswith("Value error, "):
            message = message[len("Value error, ") :]
        parts.append(f"{location}: {message}")

    summary = "; ".join(parts[:max_errors])
    if len(parts) > max_errors:
        summary += f" (+{len(parts) - max_errors} more)"
    if len(summary) > MAX_REASON_LENGTH:
        summary = summary[: MAX_REASON_LENGTH - 3] + "..."
    return summary or "validation failed"
