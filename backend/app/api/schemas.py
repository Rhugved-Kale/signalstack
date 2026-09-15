"""Pydantic v2 response models for the HTTP API.

Money is `Decimal` everywhere behind this boundary. JSON has no decimal type,
so the API is where it becomes a number: `JsonDecimal` serialises to a JSON
number rather than Pydantic v2's default string, configured once here and
reused by every schema. The exact `Decimal` still governs everything that
matters — the database columns and the attribution arithmetic.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# ---------------------------------------------------------------------------
# Shared field types
# ---------------------------------------------------------------------------


def _decimal_to_number(value: Decimal | None) -> float | None:
    """Render a Decimal as a JSON number. Applied only in JSON mode."""
    return None if value is None else float(value)


#: A Decimal that serialises as a JSON number (e.g. 124804.39, not "124804.39").
JsonDecimal = Annotated[
    Decimal,
    PlainSerializer(_decimal_to_number, return_type=float, when_used="json"),
]

#: Same, but nullable.
OptionalJsonDecimal = Annotated[
    Decimal | None,
    PlainSerializer(_decimal_to_number, return_type=float | None, when_used="json"),
]


class ApiModel(BaseModel):
    """Base for every response schema."""

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelName(str, Enum):
    """The attribution models. An Enum so FastAPI documents the valid values
    and rejects anything else with a 422 that lists them."""

    last_touch = "last_touch"
    first_touch = "first_touch"
    linear = "linear"
    time_decay = "time_decay"
    position_based = "position_based"


class Granularity(str, Enum):
    day = "day"
    week = "week"
    month = "month"


class FailureProfile(str, Enum):
    none = "none"
    normal = "normal"
    chaos = "chaos"


class ModelInfo(ApiModel):
    name: ModelName
    label: str = Field(description="Human-readable name for a UI selector")
    description: str = Field(description="What this model assumes about marketing")
    favours: str = Field(description="Which part of the funnel it tends to credit")


class ModelsResponse(ApiModel):
    models: list[ModelInfo]


# ---------------------------------------------------------------------------
# Channel performance
# ---------------------------------------------------------------------------


class ChannelPerformance(ApiModel):
    channel: str
    attributed_revenue: JsonDecimal
    attributed_conversions: JsonDecimal = Field(
        description="Sum of credit, so fractional: a shared conversion is split"
    )
    spend: JsonDecimal
    # Null rather than 0 when there was no spend — organic can never have a ROAS.
    roas: float | None = None
    touchpoint_count: int


class ChannelsResponse(ApiModel):
    model: ModelName
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    data: list[ChannelPerformance]


# ---------------------------------------------------------------------------
# Model comparison
# ---------------------------------------------------------------------------


class ChannelComparison(ApiModel):
    channel: str
    by_model: dict[str, JsonDecimal]
    max_revenue: JsonDecimal
    min_revenue: JsonDecimal
    swing: JsonDecimal = Field(
        description="max minus min across models: revenue whose owner is disputed"
    )
    swing_pct: JsonDecimal
    most_generous_model: str
    least_generous_model: str


class ModelComparisonResponse(ApiModel):
    models: list[str]
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    channels: list[ChannelComparison]
    totals_by_model: dict[str, JsonDecimal]
    total_swing: JsonDecimal


# ---------------------------------------------------------------------------
# Timeseries
# ---------------------------------------------------------------------------


class TimeseriesPoint(ApiModel):
    period: dt.date
    channel: str
    attributed_revenue: JsonDecimal
    attributed_conversions: JsonDecimal


class TimeseriesResponse(ApiModel):
    model: ModelName
    granularity: Granularity
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    data: list[TimeseriesPoint]


# ---------------------------------------------------------------------------
# Journeys
# ---------------------------------------------------------------------------


class JourneyTouchpoint(ApiModel):
    touchpoint_id: int
    channel: str
    touch_type: str
    occurred_at: dt.datetime
    credit: JsonDecimal
    attributed_revenue: JsonDecimal


class JourneyDetail(ApiModel):
    conversion_id: int
    conversion_external_id: str
    user_id: str
    converted_at: dt.datetime
    revenue: JsonDecimal
    touchpoint_count: int
    path: str = Field(description="Channel path, e.g. 'display -> email -> paid_search'")
    touchpoints: list[JourneyTouchpoint]


class JourneysResponse(ApiModel):
    model: ModelName
    limit: int
    data: list[JourneyDetail]


# ---------------------------------------------------------------------------
# Pipeline health
# ---------------------------------------------------------------------------


class IngestionRunSummary(ApiModel):
    id: int
    source: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    records_received: int
    records_ingested: int
    records_quarantined: int
    retry_count: int
    error_message: str | None = None


class QuarantineReason(ApiModel):
    source: str
    error_reason: str
    count: int


class QuarantineBreakdown(ApiModel):
    total: int
    by_source: dict[str, int]
    by_reason: list[QuarantineReason]


class PipelineHealthResponse(ApiModel):
    recent_runs: list[IngestionRunSummary]
    quarantine: QuarantineBreakdown
    row_counts: dict[str, int]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class DateRange(ApiModel):
    start: dt.date | None = None
    end: dt.date | None = None


class LargestDisagreement(ApiModel):
    channel: str | None = None
    swing: OptionalJsonDecimal = None
    most_generous_model: str | None = None
    least_generous_model: str | None = None


class SummaryResponse(ApiModel):
    model: ModelName
    total_attributed_revenue: JsonDecimal
    total_spend: JsonDecimal
    blended_roas: float | None = None
    conversion_count: int
    touchpoint_count: int
    channel_count: int
    date_range: DateRange
    largest_disagreement: LargestDisagreement


# ---------------------------------------------------------------------------
# Demo reset
# ---------------------------------------------------------------------------


class DemoResetRequest(ApiModel):
    seed: int = Field(default=42, ge=0, description="World seed")
    users: int = Field(
        default=3000,
        ge=1,
        le=20_000,
        description="Users to generate. Capped to keep the demo button quick.",
    )
    failure_profile: FailureProfile = FailureProfile.normal


class JobStatus(str, Enum):
    running = "running"
    success = "success"
    failed = "failed"


class StageState(ApiModel):
    name: str
    status: str
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    detail: dict[str, Any] | None = None


class DemoJobResponse(ApiModel):
    job_id: str
    status: JobStatus
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    params: dict[str, Any]
    stages: list[StageState]
    error: str | None = None
    duration_seconds: float | None = None


class ErrorResponse(ApiModel):
    error: str
    detail: str
