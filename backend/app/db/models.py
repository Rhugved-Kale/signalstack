"""SQLAlchemy ORM models for SignalStack.

Conventions used throughout:
  * SQLAlchemy 2.0 declarative style (``Mapped`` / ``mapped_column``).
  * Every timestamp is timezone-aware (``DateTime(timezone=True)``).
  * Every monetary amount is ``Numeric(12, 2)`` — never a float.
  * Every table has an integer surrogate primary key named ``id``.

``datetime`` is imported as ``dt`` so that the ``AdSpend.date`` column name does
not shadow the ``date`` type used in the annotations.
"""

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Campaign(Base):
    """An ad campaign as reported by an upstream ad platform."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The id the upstream ad platform gives us; our idempotency key on re-ingest.
    external_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # e.g. google_ads, meta_ads, tiktok, email, organic
    channel: Mapped[str] = mapped_column(String, nullable=False)
    # Which source system the record came from.
    platform: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdSpend(Base):
    """Daily spend/impression/click totals for one campaign."""

    __tablename__ = "ad_spend"
    __table_args__ = (
        # Makes re-ingestion of the same campaign-day idempotent.
        UniqueConstraint("campaign_id", "date", name="uq_ad_spend_campaign_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False, index=True
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    spend: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False)
    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Touchpoint(Base):
    """A single marketing interaction by a visitor/customer."""

    __tablename__ = "touchpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # The visitor/customer identifier used to stitch journeys together.
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Nullable: organic/direct touches belong to no campaign.
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # click, impression, email_open, etc.
    touch_type: Mapped[str] = mapped_column(String, nullable=False)
    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Conversion(Base):
    """A revenue event to be attributed back to touchpoints."""

    __tablename__ = "conversions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The order id from the payment API.
    external_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revenue: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String, nullable=False, default="USD", server_default=text("'USD'")
    )
    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AttributionResult(Base):
    """Credit assigned to one touchpoint for one conversion, under one model."""

    __tablename__ = "attribution_results"
    __table_args__ = (
        UniqueConstraint(
            "conversion_id",
            "touchpoint_id",
            "model_name",
            name="uq_attribution_conversion_touchpoint_model",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversion_id: Mapped[int] = mapped_column(
        ForeignKey("conversions.id"), nullable=False, index=True
    )
    touchpoint_id: Mapped[int] = mapped_column(
        ForeignKey("touchpoints.id"), nullable=False, index=True
    )
    # last_touch, first_touch, linear, time_decay, position_based
    model_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Fraction of the conversion credited to this touchpoint, 0..1.
    credit: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    attributed_revenue: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IngestionRun(Base):
    """One execution of an ingestion job against one upstream source."""

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Which upstream API we pulled from.
    source: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # running, success, failed, partial
    status: Mapped[str] = mapped_column(String, nullable=False)
    records_received: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    records_ingested: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    records_quarantined: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class QuarantinedRecord(Base):
    """A record that failed validation, kept verbatim for later replay."""

    __tablename__ = "quarantined_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingestion_run_id: Mapped[int] = mapped_column(
        ForeignKey("ingestion_runs.id"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    # The exact bad record we received, unmodified.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error_reason: Mapped[str] = mapped_column(Text, nullable=False)
    quarantined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
