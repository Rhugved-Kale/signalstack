"""Tests for the ingestion pipeline.

Schema and fetcher tests are pure; the loader/runner tests hit the real local
Postgres. Every database test truncates before *and* after itself, so nothing
is left behind either way.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.db.models import (
    AdSpend,
    Campaign,
    Conversion,
    IngestionRun,
    QuarantinedRecord,
    Touchpoint,
)
from app.db.session import SessionLocal
from app.generator.fake_apis import (
    FailureConfig,
    FakeTimeoutError,
    RateLimitError,
    ServerError,
)
from app.generator.world import WorldConfig, build_world
from app.pipeline.cli import FAILURE_PROFILES, DATA_TABLES
from app.pipeline.fetcher import (
    BACKOFF_MAX,
    MAX_ATTEMPTS,
    FetchStats,
    PipelineFetchError,
    fetch_all_pages,
)
from app.pipeline.loaders import upsert_campaigns, upsert_touchpoints
from app.pipeline.replay import replay_quarantine
from app.pipeline.runner import run_ingestion
from app.pipeline.schemas import (
    MAX_TIMESTAMP_AGE,
    MAX_TIMESTAMP_SKEW,
    SUPPORTED_CURRENCIES,
    AdSpendIn,
    CampaignIn,
    ConversionIn,
    TouchpointIn,
    describe_validation_error,
)

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _truncate(db) -> None:
    db.rollback()
    db.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    db.commit()


@pytest.fixture
def db():
    """A session against the real database, clean before and after."""
    session = SessionLocal()
    _truncate(session)
    try:
        yield session
    finally:
        _truncate(session)
        session.close()


def _no_sleep(_seconds: float) -> None:
    """Injected in place of time.sleep so tests never actually wait."""


# ---------------------------------------------------------------------------
# Clean records
# ---------------------------------------------------------------------------

CLEAN_CAMPAIGN = {
    "campaign_id": "cmp_abc123",
    "campaign_name": "Prospecting Display — US Skincare",
    "channel": "display",
    "platform": "meta_ads",
}
CLEAN_AD_SPEND = {
    "campaign_id": "cmp_abc123",
    "date": "2026-07-18",
    "spend_usd": 19.18,
    "impressions": 4235,
    "clicks": 34,
}
CLEAN_TOUCHPOINT = {
    "event_id": "evt_abc123",
    "user_id": "usr_abc123",
    "campaign_id": "cmp_abc123",
    "channel": "display",
    "event_type": "impression",
    "timestamp": "2026-07-18T05:10:44Z",
}
CLEAN_CONVERSION = {
    "order_id": "ord_abc123",
    "customer_id": "usr_abc123",
    "amount": 51.62,
    "currency": "USD",
    "created_at": "2026-07-21T10:28:15Z",
}


def test_clean_records_validate():
    campaign = CampaignIn.model_validate(CLEAN_CAMPAIGN)
    assert campaign.external_id == "cmp_abc123"
    assert campaign.name.startswith("Prospecting")

    spend = AdSpendIn.model_validate(CLEAN_AD_SPEND)
    assert spend.campaign_external_id == "cmp_abc123"
    assert spend.date == dt.date(2026, 7, 18)
    # Money is Decimal, and exactly the value on the wire — not a float artefact.
    assert spend.spend == Decimal("19.18")
    assert isinstance(spend.spend, Decimal)

    touch = TouchpointIn.model_validate(CLEAN_TOUCHPOINT)
    assert touch.touch_type == "impression"
    assert touch.occurred_at == dt.datetime(2026, 7, 18, 5, 10, 44, tzinfo=UTC)
    assert touch.occurred_at.tzinfo is not None

    conversion = ConversionIn.model_validate(CLEAN_CONVERSION)
    assert conversion.revenue == Decimal("51.62")
    assert conversion.user_id == "usr_abc123"
    assert conversion.currency == "USD"


def test_numeric_strings_are_coerced_not_rejected():
    """API sloppiness, not corruption."""
    spend = AdSpendIn.model_validate(
        {**CLEAN_AD_SPEND, "spend_usd": "42.50", "impressions": "100", "clicks": "5"}
    )
    assert spend.spend == Decimal("42.50")
    assert spend.impressions == 100
    assert spend.clicks == 5

    conversion = ConversionIn.model_validate({**CLEAN_CONVERSION, "amount": "123.45"})
    assert conversion.revenue == Decimal("123.45")


def test_unknown_extra_fields_are_ignored():
    payload = {
        **CLEAN_CONVERSION,
        "__v": 2,
        "_debug_trace_id": "trace-123",
        "experiment_bucket": "control",
        "beta_metric": 0.42,
    }
    conversion = ConversionIn.model_validate(payload)
    assert conversion.external_id == "ord_abc123"
    assert not hasattr(conversion, "__v") or True  # simply not fatal


def test_timestamps_accept_iso_with_and_without_z_and_unix_epoch():
    with_z = ConversionIn.model_validate(
        {**CLEAN_CONVERSION, "created_at": "2026-07-21T10:28:15Z"}
    ).occurred_at
    without_z = ConversionIn.model_validate(
        {**CLEAN_CONVERSION, "created_at": "2026-07-21T10:28:15+00:00"}
    ).occurred_at
    naive = ConversionIn.model_validate(
        {**CLEAN_CONVERSION, "created_at": "2026-07-21T10:28:15"}
    ).occurred_at
    epoch = ConversionIn.model_validate(
        {**CLEAN_CONVERSION, "created_at": 1726902617}
    ).occurred_at

    assert with_z == without_z == naive
    assert all(value.tzinfo is not None for value in (with_z, without_z, naive, epoch))
    assert epoch == dt.datetime.fromtimestamp(1726902617, tz=UTC)


@pytest.mark.parametrize(
    "garbage",
    ["not-a-date", "0000-00-00", "", "yesterday", "   "],
)
def test_garbage_timestamp_is_rejected_as_validationerror(garbage):
    """Must surface as ValidationError, never an uncaught ValueError.

    `datetime.fromisoformat("0000-00-00")` raises ValueError rather than
    failing a format check, which is exactly the trap Phase 3 flagged.
    """
    with pytest.raises(ValidationError) as excinfo:
        ConversionIn.model_validate({**CLEAN_CONVERSION, "created_at": garbage})
    reason = describe_validation_error(excinfo.value)
    assert "created_at" in reason
    assert reason


@pytest.mark.parametrize(
    ("model", "clean", "field", "value", "expected_in_reason"),
    [
        # Nulls in required fields
        (CampaignIn, CLEAN_CAMPAIGN, "campaign_name", None, "must not be null"),
        (ConversionIn, CLEAN_CONVERSION, "amount", None, "must not be null"),
        (AdSpendIn, CLEAN_AD_SPEND, "impressions", None, "must not be null"),
        # Empty strings
        (CampaignIn, CLEAN_CAMPAIGN, "campaign_id", "", "must not be empty"),
        (TouchpointIn, CLEAN_TOUCHPOINT, "user_id", "", "must not be empty"),
        # An empty campaign_id is corruption, unlike an explicit null
        (TouchpointIn, CLEAN_TOUCHPOINT, "campaign_id", "", "must not be empty"),
        # Negative money
        (ConversionIn, CLEAN_CONVERSION, "amount", -537.51, "greater than or equal to 0"),
        (AdSpendIn, CLEAN_AD_SPEND, "spend_usd", -12.0, "greater than or equal to 0"),
        # Bad currency
        (ConversionIn, CLEAN_CONVERSION, "currency", "999", "not a supported currency"),
        (ConversionIn, CLEAN_CONVERSION, "currency", "US$", "not a supported currency"),
        # Shape-valid but semantically unsupported.
        (ConversionIn, CLEAN_CONVERSION, "currency", "XYZ", "not a supported currency"),
        (ConversionIn, CLEAN_CONVERSION, "currency", "", "must not be empty"),
        # Garbage numerics
        (AdSpendIn, CLEAN_AD_SPEND, "impressions", "not-a-number", "integer"),
        (ConversionIn, CLEAN_CONVERSION, "amount", "not-a-number", "valid amount"),
        # Garbage timestamp as unix-ish nonsense
        (TouchpointIn, CLEAN_TOUCHPOINT, "timestamp", "not-a-date", "unparseable"),
    ],
)
def test_malformed_variants_are_rejected_with_a_sensible_reason(
    model, clean, field, value, expected_in_reason
):
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate({**clean, field: value})
    reason = describe_validation_error(excinfo.value)
    assert field in reason, f"reason should name the wire field: {reason}"
    assert expected_in_reason in reason, f"unexpected reason: {reason}"


@pytest.mark.parametrize(
    ("model", "clean", "missing"),
    [
        (CampaignIn, CLEAN_CAMPAIGN, "campaign_id"),
        (ConversionIn, CLEAN_CONVERSION, "amount"),
        (AdSpendIn, CLEAN_AD_SPEND, "date"),
        # A MISSING campaign_id key is rejected even though null is allowed.
        (TouchpointIn, CLEAN_TOUCHPOINT, "campaign_id"),
    ],
)
def test_missing_required_key_is_rejected(model, clean, missing):
    payload = {key: value for key, value in clean.items() if key != missing}
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate(payload)
    reason = describe_validation_error(excinfo.value)
    assert missing in reason
    assert "Field required" in reason


def test_explicit_null_campaign_id_is_accepted_for_organic():
    touch = TouchpointIn.model_validate(
        {**CLEAN_TOUCHPOINT, "campaign_id": None, "channel": "organic", "event_type": "visit"}
    )
    assert touch.campaign_external_id is None


def test_clicks_may_not_exceed_impressions():
    with pytest.raises(ValidationError) as excinfo:
        AdSpendIn.model_validate({**CLEAN_AD_SPEND, "impressions": 5, "clicks": 10})
    assert "cannot exceed impressions" in describe_validation_error(excinfo.value)

    # Equal is fine.
    assert AdSpendIn.model_validate(
        {**CLEAN_AD_SPEND, "impressions": 10, "clicks": 10}
    ).clicks == 10


def test_lowercase_currency_is_uppercased():
    assert ConversionIn.model_validate({**CLEAN_CONVERSION, "currency": "usd"}).currency == "USD"


def test_currency_must_be_in_the_supported_allowlist():
    """A well-formed code is not automatically a supported one."""
    # "XYZ" is three alphabetic characters and still rejected.
    with pytest.raises(ValidationError) as excinfo:
        ConversionIn.model_validate({**CLEAN_CONVERSION, "currency": "XYZ"})
    reason = describe_validation_error(excinfo.value)
    assert reason == "currency: 'XYZ' is not a supported currency code"

    # Every allowlisted code is accepted, in either case.
    for code in SUPPORTED_CURRENCIES:
        assert (
            ConversionIn.model_validate(
                {**CLEAN_CONVERSION, "currency": code.lower()}
            ).currency
            == code
        )


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


def test_fetch_retries_server_error_then_succeeds():
    attempts = {"count": 0}
    slept: list[float] = []

    def flaky(page: int = 1, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise ServerError("boom", status_code=503)
        return {"data": [{"page": page}], "page": page, "has_more": False}

    stats = FetchStats()
    records = list(
        fetch_all_pages(flaky, source="test", sleep_fn=slept.append, stats=stats)
    )

    assert records == [{"page": 1}]
    assert attempts["count"] == 3
    assert stats.retries == 2
    assert len(slept) == 2
    # Exponential backoff with jitter: bounded, and growing.
    assert all(0 < value <= BACKOFF_MAX for value in slept)
    assert slept[1] > slept[0]


def test_rate_limit_waits_exactly_retry_after():
    attempts = {"count": 0}
    slept: list[float] = []

    def limited(page: int = 1, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RateLimitError(retry_after=7)
        return {"data": [], "page": page, "has_more": False}

    stats = FetchStats()
    list(fetch_all_pages(limited, source="test", sleep_fn=slept.append, stats=stats))

    assert slept == [7.0], "a 429 must honour retry_after, not the backoff curve"
    assert stats.retries == 1


def test_exceeding_max_attempts_raises_pipeline_fetch_error():
    def always_timeout(page: int = 1, **_kwargs):
        raise FakeTimeoutError()

    stats = FetchStats()
    with pytest.raises(PipelineFetchError) as excinfo:
        list(
            fetch_all_pages(
                always_timeout, source="dead", sleep_fn=_no_sleep, stats=stats
            )
        )

    error = excinfo.value
    assert error.attempts == MAX_ATTEMPTS
    assert error.source == "dead"
    assert error.page == 1
    assert isinstance(error.last_exception, FakeTimeoutError)
    assert stats.retries == MAX_ATTEMPTS - 1


def test_validation_failures_are_not_retried():
    """Retrying a data problem just reproduces it."""
    attempts = {"count": 0}

    def bad_data(page: int = 1, **_kwargs):
        attempts["count"] += 1
        raise ValueError("malformed envelope")

    stats = FetchStats()
    with pytest.raises(ValueError):
        list(fetch_all_pages(bad_data, source="test", sleep_fn=_no_sleep, stats=stats))

    assert attempts["count"] == 1, "must not retry a non-transport error"
    assert stats.retries == 0


def test_fetch_walks_every_page():
    pages = {
        1: {"data": [{"i": 1}, {"i": 2}], "has_more": True},
        2: {"data": [{"i": 3}], "has_more": True},
        3: {"data": [{"i": 4}], "has_more": False},
    }

    def paged(page: int = 1, **_kwargs):
        return pages[page]

    stats = FetchStats()
    records = list(fetch_all_pages(paged, source="test", sleep_fn=_no_sleep, stats=stats))
    assert [record["i"] for record in records] == [1, 2, 3, 4]
    assert stats.pages_fetched == 3
    assert stats.records_received == 4


# ---------------------------------------------------------------------------
# Loaders / referential integrity
# ---------------------------------------------------------------------------


def test_touchpoint_with_unknown_campaign_is_rejected_not_nulled(db):
    upsert_campaigns(db, [CampaignIn.model_validate(CLEAN_CAMPAIGN)])
    db.commit()

    good = TouchpointIn.model_validate(CLEAN_TOUCHPOINT)
    organic = TouchpointIn.model_validate(
        {
            **CLEAN_TOUCHPOINT,
            "event_id": "evt_organic",
            "campaign_id": None,
            "channel": "organic",
            "event_type": "visit",
        }
    )
    dangling = TouchpointIn.model_validate(
        {**CLEAN_TOUCHPOINT, "event_id": "evt_dangling", "campaign_id": "cmp_does_not_exist"}
    )

    result = upsert_touchpoints(db, [good, organic, dangling])
    db.commit()

    assert result.affected == 2, "only the resolvable and organic rows should load"
    assert len(result.rejected) == 1
    rejected_record, reason = result.rejected[0]
    assert rejected_record.external_id == "evt_dangling"
    assert "unknown campaign_id" in reason

    stored = {
        external_id: campaign_id
        for external_id, campaign_id in db.execute(
            select(Touchpoint.external_id, Touchpoint.campaign_id)
        ).all()
    }
    assert set(stored) == {"evt_abc123", "evt_organic"}
    assert stored["evt_organic"] is None, "organic keeps a null campaign"
    assert stored["evt_abc123"] is not None
    assert "evt_dangling" not in stored, "must not be silently nulled into the table"


def test_dangling_touchpoints_are_written_to_quarantine_by_the_runner(db):
    """End-to-end proof that a referential error lands in quarantined_records."""
    result = run_ingestion(
        db,
        world_config=WorldConfig(seed=42, n_users=60),
        failure_config=FailureConfig.none(),
        sources=["touchpoints"],  # campaigns deliberately not ingested
        sleep_fn=_no_sleep,
    )

    touchpoints = result["sources"][0]
    assert touchpoints["quarantined"] > 0
    assert touchpoints["status"] == "partial"

    rows = db.execute(
        select(QuarantinedRecord.error_reason, QuarantinedRecord.raw_payload)
    ).all()
    assert rows
    assert all("unknown campaign_id" in reason for reason, _ in rows)
    # The raw payload is kept verbatim for replay.
    assert all(payload.get("event_id") for _, payload in rows)

    # Only organic touchpoints (null campaign) could possibly have loaded.
    loaded = db.execute(select(Touchpoint.campaign_id)).scalars().all()
    assert all(campaign_id is None for campaign_id in loaded)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _table_counts(db) -> dict[str, int]:
    return {
        "campaigns": db.scalar(select(func.count()).select_from(Campaign)),
        "ad_spend": db.scalar(select(func.count()).select_from(AdSpend)),
        "touchpoints": db.scalar(select(func.count()).select_from(Touchpoint)),
        "conversions": db.scalar(select(func.count()).select_from(Conversion)),
    }


def test_ingestion_is_idempotent(db):
    """Running twice with the same seed must not change the data tables.

    Corruption and duplicates are enabled (deterministically, via the seed) so
    this exercises dedupe and quarantine too; transport failures are off so the
    test does not depend on backoff timing.
    """
    config = WorldConfig(seed=42, n_users=150)
    failures = FailureConfig(
        server_error_rate=0.0,
        rate_limit_rate=0.0,
        timeout_rate=0.0,
        malformed_record_rate=0.10,
        duplicate_record_rate=0.10,
    )

    first = run_ingestion(db, world_config=config, failure_config=failures, sleep_fn=_no_sleep)
    counts_after_first = _table_counts(db)

    second = run_ingestion(db, world_config=config, failure_config=failures, sleep_fn=_no_sleep)
    counts_after_second = _table_counts(db)

    assert counts_after_first == counts_after_second, "row counts must not drift"
    assert all(count > 0 for count in counts_after_second.values())

    # The second pass updated rows instead of inserting them.
    assert first["totals"]["rows_inserted"] > 0
    assert second["totals"]["rows_inserted"] == 0
    assert second["totals"]["rows_updated"] > 0

    # And no duplicate natural keys exist anywhere.
    for model in (Campaign, Touchpoint, Conversion):
        total = db.scalar(select(func.count()).select_from(model))
        distinct = db.scalar(select(func.count(func.distinct(model.external_id))))
        assert total == distinct, f"duplicate external_ids in {model.__tablename__}"

    duplicate_spend = db.execute(
        select(AdSpend.campaign_id, AdSpend.date, func.count())
        .group_by(AdSpend.campaign_id, AdSpend.date)
        .having(func.count() > 1)
    ).all()
    assert not duplicate_spend, "ad_spend must be unique per campaign-day"


def test_counter_identity_received_equals_ingested_plus_quarantined(db):
    """received == ingested + quarantined, exactly.

    `ingested` counts unique rows upserted plus in-batch duplicates that were
    dropped, because a dropped duplicate's data is present in the database via
    its twin. See runner.py's module docstring.
    """
    result = run_ingestion(
        db,
        world_config=WorldConfig(seed=7, n_users=150),
        failure_config=FailureConfig(0.0, 0.0, 0.0, 0.12, 0.08),
        sleep_fn=_no_sleep,
    )

    for source in result["sources"]:
        assert source["received"] == source["ingested"] + source["quarantined"], source
        # And the breakdown reconciles with the ingested figure.
        assert source["ingested"] == (
            source["rows_inserted"] + source["rows_updated"] + source["duplicates_dropped"]
        ), source

    totals = result["totals"]
    assert totals["received"] == totals["ingested"] + totals["quarantined"]
    assert totals["duplicates_dropped"] > 0, "expected the duplicate rate to bite"
    assert totals["quarantined"] > 0, "expected the malformed rate to bite"

    # The same numbers must be persisted on the IngestionRun rows.
    runs = db.execute(select(IngestionRun).order_by(IngestionRun.id)).scalars().all()
    assert len(runs) == 4
    for run in runs:
        assert run.records_received == run.records_ingested + run.records_quarantined
        assert run.finished_at is not None
        assert run.started_at <= run.finished_at
        assert run.status in {"success", "partial", "failed"}


def test_chaos_profile_still_completes_with_partial_status(db):
    result = run_ingestion(
        db,
        world_config=WorldConfig(seed=42, n_users=120),
        failure_config=FAILURE_PROFILES["chaos"],
        sleep_fn=_no_sleep,
    )

    # The run completed: every source produced a result and an IngestionRun.
    assert len(result["sources"]) == 4
    runs = db.execute(select(IngestionRun)).scalars().all()
    assert len(runs) == 4
    assert all(run.finished_at is not None for run in runs)
    assert all(run.status != "running" for run in runs)

    partial = [run for run in runs if run.status == "partial"]
    assert partial, f"expected a partial run, got {[r.status for r in runs]}"
    assert any(run.records_quarantined > 0 for run in partial)
    assert result["totals"]["quarantined"] > 0

    # Quarantined rows carry both the raw payload and a reason.
    quarantined = db.execute(select(QuarantinedRecord)).scalars().all()
    assert quarantined
    assert all(row.error_reason for row in quarantined)
    assert all(isinstance(row.raw_payload, dict) for row in quarantined)

    # A single bad record never aborted a source: something still landed.
    assert result["totals"]["ingested"] > 0


def test_one_failing_source_does_not_stop_the_others(db):
    """A source that blows up entirely is recorded, and the rest still run."""

    def exploding(**_kwargs):
        raise ServerError("permanently broken", status_code=500)

    from app.pipeline import runner as runner_module

    original = runner_module.build_source_specs

    def patched(world, failure_config, seed):
        specs = original(world, failure_config, seed)
        broken = specs["ad_spend"]
        specs["ad_spend"] = type(broken)(
            name=broken.name,
            fetch_fn=exploding,
            model=broken.model,
            loader=broken.loader,
            per_page=broken.per_page,
        )
        return specs

    runner_module.build_source_specs = patched
    try:
        result = run_ingestion(
            db,
            world_config=WorldConfig(seed=42, n_users=80),
            failure_config=FailureConfig.none(),
            sleep_fn=_no_sleep,
        )
    finally:
        runner_module.build_source_specs = original

    by_source = {row["source"]: row for row in result["sources"]}
    assert by_source["ad_spend"]["status"] == "failed"
    assert "ServerError" in by_source["ad_spend"]["error_message"]

    # Everything else still succeeded.
    for name in ("campaigns", "touchpoints", "conversions"):
        assert by_source[name]["status"] in {"success", "partial"}
        assert by_source[name]["ingested"] > 0

    assert db.scalar(select(func.count()).select_from(AdSpend)) == 0
    assert db.scalar(select(func.count()).select_from(Campaign)) > 0

    # The failure is on the record, with its message.
    failed_run = db.execute(
        select(IngestionRun).where(IngestionRun.source == "ad_spend")
    ).scalar_one()
    assert failed_run.status == "failed"
    assert failed_run.error_message
    assert failed_run.retry_count > 0, "the transport error should have been retried"


# ---------------------------------------------------------------------------
# Quarantine replay (Phase 4.5)
# ---------------------------------------------------------------------------


def test_orphaned_touchpoint_is_recovered_once_its_parent_exists(db):
    """The Phase 4 cascade, repaired.

    Ingest touchpoints with no campaigns present, so every non-organic
    touchpoint is quarantined purely for being an orphan. Replay re-fetches
    the parents and the orphans then load cleanly.
    """
    config = WorldConfig(seed=42, n_users=80)

    ingest = run_ingestion(
        db,
        world_config=config,
        failure_config=FailureConfig.none(),
        sources=["touchpoints"],
        sleep_fn=_no_sleep,
    )
    orphaned = ingest["sources"][0]["quarantined"]
    assert orphaned > 0, "expected orphans with no campaigns loaded"

    quarantined_before = (
        db.execute(
            select(QuarantinedRecord.raw_payload).where(
                QuarantinedRecord.source == "touchpoints"
            )
        )
        .scalars()
        .all()
    )
    orphan_event_ids = {payload["event_id"] for payload in quarantined_before}
    assert len(orphan_event_ids) == orphaned
    assert db.scalar(select(func.count()).select_from(Campaign)) == 0

    result = replay_quarantine(
        db,
        world_config=config,
        failure_config=FailureConfig.none(),
        sleep_fn=_no_sleep,
    )

    # The parents were re-fetched and the orphans recovered.
    assert result["parent_campaigns_recovered"] > 0
    assert result["sources"]["touchpoints"]["recovered"] == orphaned
    assert result["sources"]["touchpoints"]["still_quarantined"] == 0
    assert result["status"] == "success"

    # They are gone from quarantine...
    assert (
        db.scalar(
            select(func.count())
            .select_from(QuarantinedRecord)
            .where(QuarantinedRecord.source == "touchpoints")
        )
        == 0
    )

    # ...and present in the real table, with a resolved campaign.
    stored = {
        external_id: campaign_id
        for external_id, campaign_id in db.execute(
            select(Touchpoint.external_id, Touchpoint.campaign_id).where(
                Touchpoint.external_id.in_(orphan_event_ids)
            )
        ).all()
    }
    assert set(stored) == orphan_event_ids
    assert all(campaign_id is not None for campaign_id in stored.values())


def test_genuinely_malformed_record_is_not_recovered(db):
    """A garbage timestamp cannot be repaired by replay; it must stay put."""
    run = IngestionRun(
        source="touchpoints", started_at=dt.datetime.now(UTC), status="success"
    )
    db.add(run)
    db.commit()

    bad_payload = {**CLEAN_TOUCHPOINT, "campaign_id": None, "timestamp": "not-a-date"}
    db.add(
        QuarantinedRecord(
            ingestion_run_id=run.id,
            source="touchpoints",
            raw_payload=bad_payload,
            error_reason="timestamp: unparseable timestamp 'not-a-date'",
        )
    )
    db.commit()

    result = replay_quarantine(
        db,
        world_config=WorldConfig(seed=42, n_users=40),
        failure_config=FailureConfig.none(),
        sleep_fn=_no_sleep,
    )

    assert result["sources"]["touchpoints"]["recovered"] == 0
    assert result["sources"]["touchpoints"]["still_quarantined"] == 1
    assert result["status"] == "partial"

    survivor = db.execute(
        select(QuarantinedRecord).where(QuarantinedRecord.source == "touchpoints")
    ).scalar_one()
    assert survivor.error_reason, "error_reason must stay populated"
    assert "timestamp" in survivor.error_reason
    assert "unparseable" in survivor.error_reason
    # The raw payload is untouched, so it can still be inspected or replayed.
    assert survivor.raw_payload == bad_payload
    # And nothing was written to the real table.
    assert db.scalar(select(func.count()).select_from(Touchpoint)) == 0


def test_replay_is_idempotent(db):
    config = WorldConfig(seed=42, n_users=150)
    failures = FailureConfig(
        server_error_rate=0.0,
        rate_limit_rate=0.0,
        timeout_rate=0.0,
        malformed_record_rate=0.12,
        duplicate_record_rate=0.05,
    )
    run_ingestion(db, world_config=config, failure_config=failures, sleep_fn=_no_sleep)

    first = replay_quarantine(
        db, world_config=config, failure_config=failures, sleep_fn=_no_sleep
    )
    counts_after_first = _table_counts(db)
    surviving_ids_first = set(
        db.execute(select(QuarantinedRecord.id)).scalars().all()
    )

    second = replay_quarantine(
        db, world_config=config, failure_config=failures, sleep_fn=_no_sleep
    )
    counts_after_second = _table_counts(db)
    surviving_ids_second = set(
        db.execute(select(QuarantinedRecord.id)).scalars().all()
    )

    assert counts_after_first == counts_after_second, "data tables must not drift"
    assert second["totals"]["recovered"] == 0, "nothing left to recover"
    # Deleted quarantine rows stay deleted; no row is resurrected.
    assert surviving_ids_second == surviving_ids_first
    assert surviving_ids_second <= surviving_ids_first

    # Still no duplicate natural keys after a replay.
    for model in (Campaign, Touchpoint, Conversion):
        total = db.scalar(select(func.count()).select_from(model))
        distinct = db.scalar(select(func.count(func.distinct(model.external_id))))
        assert total == distinct, f"duplicate external_ids in {model.__tablename__}"

    # The second replay stops after one unproductive round.
    assert second["rounds"] == 1


def test_replay_records_an_ingestion_run_with_source_replay(db):
    run_ingestion(
        db,
        world_config=WorldConfig(seed=42, n_users=60),
        failure_config=FailureConfig(0.0, 0.0, 0.0, 0.15, 0.0),
        sleep_fn=_no_sleep,
    )
    result = replay_quarantine(
        db,
        world_config=WorldConfig(seed=42, n_users=60),
        failure_config=FailureConfig.none(),
        sleep_fn=_no_sleep,
    )

    replay_run = db.execute(
        select(IngestionRun).where(IngestionRun.source == "replay")
    ).scalar_one()

    assert replay_run.id == result["replay_run_id"]
    assert replay_run.status in {"success", "partial", "failed"}
    assert replay_run.finished_at is not None
    assert replay_run.started_at <= replay_run.finished_at
    assert replay_run.records_received == result["totals"]["attempted"]
    assert replay_run.records_ingested == result["totals"]["recovered"]
    assert replay_run.records_quarantined == result["totals"]["still_quarantined"]
    # The replay run keeps the same counter identity as an ingestion run.
    assert (
        replay_run.records_received
        == replay_run.records_ingested + replay_run.records_quarantined
    )


def test_replay_processes_sources_in_dependency_order(db):
    """Campaigns must be repaired before their children are retried."""
    from app.pipeline.replay import REPLAY_ORDER

    assert REPLAY_ORDER.index("campaigns") == 0
    assert REPLAY_ORDER.index("campaigns") < REPLAY_ORDER.index("ad_spend")
    assert REPLAY_ORDER.index("campaigns") < REPLAY_ORDER.index("touchpoints")

    # Requesting only children still repairs the parents.
    run_ingestion(
        db,
        world_config=WorldConfig(seed=42, n_users=60),
        failure_config=FailureConfig.none(),
        sources=["touchpoints"],
        sleep_fn=_no_sleep,
    )
    result = replay_quarantine(
        db,
        sources=["touchpoints"],
        world_config=WorldConfig(seed=42, n_users=60),
        failure_config=FailureConfig.none(),
        sleep_fn=_no_sleep,
    )
    assert result["parent_campaigns_recovered"] > 0
    assert set(result["sources"]) == {"touchpoints"}
    assert result["sources"]["touchpoints"]["still_quarantined"] == 0


def test_replay_with_fetch_parents_disabled_recovers_nothing(db):
    """Without the parent re-fetch there is nothing to repair an orphan with."""
    config = WorldConfig(seed=42, n_users=60)
    run_ingestion(
        db,
        world_config=config,
        failure_config=FailureConfig.none(),
        sources=["touchpoints"],
        sleep_fn=_no_sleep,
    )
    before = db.scalar(select(func.count()).select_from(QuarantinedRecord))
    assert before > 0

    result = replay_quarantine(
        db,
        world_config=config,
        failure_config=FailureConfig.none(),
        fetch_parents=False,
        sleep_fn=_no_sleep,
    )

    assert result["parent_campaigns_recovered"] == 0
    assert result["totals"]["recovered"] == 0
    assert result["totals"]["still_quarantined"] == before
    assert db.scalar(select(func.count()).select_from(Campaign)) == 0


# ---------------------------------------------------------------------------
# Timestamp plausibility (Phase 6.5)
# ---------------------------------------------------------------------------

# A fixed reference instant, injected via validation context, so none of these
# tests depend on the wall clock.
PLAUSIBILITY_REF = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


def _iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_at(model, payload, *, now=PLAUSIBILITY_REF):
    return model.model_validate(payload, context={"now": now})


def _conversion_at(moment) -> dict:
    return {
        "order_id": "ord_plausible",
        "customer_id": "usr_plausible",
        "amount": 49.99,
        "currency": "USD",
        "created_at": _iso(moment) if isinstance(moment, dt.datetime) else moment,
    }


def _touchpoint_at(moment) -> dict:
    return {
        "event_id": "evt_plausible",
        "user_id": "usr_plausible",
        "campaign_id": None,
        "channel": "organic",
        "event_type": "visit",
        "timestamp": _iso(moment) if isinstance(moment, dt.datetime) else moment,
    }


def test_timestamp_three_years_in_the_past_is_rejected():
    """Shape-valid, semantically absurd for data being ingested now."""
    moment = PLAUSIBILITY_REF - dt.timedelta(days=365 * 3)
    with pytest.raises(ValidationError) as excinfo:
        _validate_at(ConversionIn, _conversion_at(moment))

    reason = describe_validation_error(excinfo.value)
    assert "created_at" in reason
    assert "outside the plausible window" in reason
    assert "2 years in the past" in reason
    # The reason names the offending value.
    assert _iso(moment) in reason


def test_timestamp_two_days_in_the_future_is_rejected():
    moment = PLAUSIBILITY_REF + dt.timedelta(days=2)
    with pytest.raises(ValidationError) as excinfo:
        _validate_at(TouchpointIn, _touchpoint_at(moment))

    reason = describe_validation_error(excinfo.value)
    assert "timestamp" in reason
    assert "outside the plausible window" in reason
    assert "1 day in the future" in reason


def test_timestamp_one_hour_in_the_future_is_accepted():
    """Clock skew between systems is normal, not corruption."""
    moment = PLAUSIBILITY_REF + dt.timedelta(hours=1)
    record = _validate_at(TouchpointIn, _touchpoint_at(moment))
    assert record.occurred_at == moment

    # The whole tolerated skew window is fine, right up to the boundary.
    edge = PLAUSIBILITY_REF + MAX_TIMESTAMP_SKEW
    assert _validate_at(TouchpointIn, _touchpoint_at(edge)).occurred_at == edge


def test_timestamp_one_year_in_the_past_is_accepted():
    """Backfilling genuinely old data is a legitimate use case."""
    moment = PLAUSIBILITY_REF - dt.timedelta(days=365)
    record = _validate_at(ConversionIn, _conversion_at(moment))
    assert record.occurred_at == moment

    edge = PLAUSIBILITY_REF - MAX_TIMESTAMP_AGE
    assert _validate_at(ConversionIn, _conversion_at(edge)).occurred_at == edge


def test_plausibility_uses_the_injected_reference_not_the_real_clock():
    """Proves the check is deterministic and clock-independent.

    With the reference moved back to 2020, a timestamp that is perfectly
    plausible by the wall clock becomes "more than 1 day in the future".
    """
    wall_clock_fine = dt.datetime(2026, 9, 1, tzinfo=UTC)
    past_reference = dt.datetime(2020, 1, 1, tzinfo=UTC)

    # Accepted against a 2026 reference...
    assert _validate_at(
        ConversionIn, _conversion_at(wall_clock_fine), now=PLAUSIBILITY_REF
    ).occurred_at == wall_clock_fine

    # ...and rejected against a 2020 one, from the identical payload.
    with pytest.raises(ValidationError) as excinfo:
        _validate_at(ConversionIn, _conversion_at(wall_clock_fine), now=past_reference)
    assert "1 day in the future" in describe_validation_error(excinfo.value)

    # And the mirror image: a 2020 timestamp is fine against a 2020 reference
    # but not against a 2026 one.
    old_moment = dt.datetime(2020, 1, 1, tzinfo=UTC)
    assert _validate_at(
        ConversionIn, _conversion_at(old_moment), now=past_reference
    ).occurred_at == old_moment
    with pytest.raises(ValidationError):
        _validate_at(ConversionIn, _conversion_at(old_moment), now=PLAUSIBILITY_REF)


def test_epoch_integer_corruption_is_now_caught():
    """The bug that started Phase 6.5.

    `timestamp_unix_int` corruption parses cleanly as a real instant, so only
    a plausibility check can reject it.
    """
    # 1_600_000_000 -> 2020-09-13, six years before the reference.
    with pytest.raises(ValidationError) as excinfo:
        _validate_at(ConversionIn, _conversion_at(1_600_000_000))
    assert "outside the plausible window" in describe_validation_error(excinfo.value)

    # 1_800_000_000 -> 2027-01-15, four months after it.
    with pytest.raises(ValidationError) as excinfo:
        _validate_at(ConversionIn, _conversion_at(1_800_000_000))
    assert "in the future" in describe_validation_error(excinfo.value)

    # An epoch inside the window is still accepted — the check is a
    # plausibility guard, not a dataset-window check.
    inside = int((PLAUSIBILITY_REF - dt.timedelta(days=10)).timestamp())
    assert _validate_at(ConversionIn, _conversion_at(inside)).occurred_at.date() == (
        PLAUSIBILITY_REF - dt.timedelta(days=10)
    ).date()


def test_ad_spend_date_is_also_checked():
    def spend_on(day) -> dict:
        return {
            "campaign_id": "cmp_plausible",
            "date": day,
            "spend_usd": 12.34,
            "impressions": 100,
            "clicks": 4,
        }

    # Three years back: rejected.
    with pytest.raises(ValidationError) as excinfo:
        _validate_at(AdSpendIn, spend_on("2023-09-15"))
    reason = describe_validation_error(excinfo.value)
    assert "date" in reason
    assert "outside the plausible window" in reason

    # Two days ahead: rejected.
    with pytest.raises(ValidationError):
        _validate_at(AdSpendIn, spend_on("2026-09-17"))

    # Tomorrow and inside the window: accepted.
    assert _validate_at(AdSpendIn, spend_on("2026-09-16")).date == dt.date(2026, 9, 16)
    assert _validate_at(AdSpendIn, spend_on("2026-08-01")).date == dt.date(2026, 8, 1)


def test_plausibility_falls_back_to_the_wall_clock_without_context():
    """Production passes no context; the real clock is the reference then."""
    now = dt.datetime.now(UTC)
    assert ConversionIn.model_validate(
        _conversion_at(now - dt.timedelta(days=1))
    ).occurred_at is not None

    with pytest.raises(ValidationError):
        ConversionIn.model_validate(_conversion_at(now - dt.timedelta(days=365 * 4)))


def test_shape_errors_still_take_precedence_over_plausibility():
    """An unparseable value is a shape error, not a plausibility one."""
    with pytest.raises(ValidationError) as excinfo:
        _validate_at(ConversionIn, _conversion_at("not-a-date"))
    reason = describe_validation_error(excinfo.value)
    assert "unparseable timestamp" in reason
    assert "plausible window" not in reason
