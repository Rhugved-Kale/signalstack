"""Tests for the HTTP API, driven through FastAPI's TestClient.

These run against the real local Postgres. Because the other test modules
truncate the data tables, this module seeds its own small dataset once and
tears it down afterwards.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api import jobs
from app.attribution.engine import run_attribution
from app.db.session import SessionLocal, get_db
from app.generator.fake_apis import FailureConfig
from app.generator.world import WorldConfig
from app.main import app
from app.pipeline.cli import reset_data
from app.pipeline.runner import run_ingestion

# Small but realistic: enough journeys that the models genuinely diverge.
SEED_CONFIG = WorldConfig(seed=42, n_users=200)
# Corruption on, transport failures off, so seeding is fast and deterministic
# but pipeline_health still has quarantine rows to report.
SEED_FAILURES = FailureConfig(
    server_error_rate=0.0,
    rate_limit_rate=0.0,
    timeout_rate=0.0,
    malformed_record_rate=0.10,
    duplicate_record_rate=0.05,
)


@pytest.fixture(scope="module", autouse=True)
def seeded_database():
    """Populate the database once for this module, then clean up."""
    db = SessionLocal()
    try:
        reset_data(db)
        run_ingestion(
            db,
            world_config=SEED_CONFIG,
            failure_config=SEED_FAILURES,
            sleep_fn=lambda _seconds: None,
        )
        run_attribution(db, rebuild=True)
        yield
    finally:
        reset_data(db)
        db.close()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_job_registry():
    jobs.registry.reset()
    yield
    jobs.registry.reset()


# ---------------------------------------------------------------------------
# Health and catalogue
# ---------------------------------------------------------------------------


def test_health_keeps_its_original_contract(client):
    """The three original keys are what deployment probes read.

    Phase 8 added a `bootstrap` key, so this asserts the original contract is
    intact rather than pinning the whole dict.
    """
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()

    assert payload["status"] == "ok"
    assert payload["service"] == "signalstack-api"
    assert payload["environment"] == "local"


def test_health_reports_bootstrap_state(client):
    payload = client.get("/health").json()
    assert payload["bootstrap"] in {
        "pending",
        "running",
        "complete",
        "skipped",
        "failed",
    }


def test_models_endpoint_lists_all_five_with_descriptions(client):
    response = client.get("/api/models")
    assert response.status_code == 200
    payload = response.json()

    assert set(payload) == {"models"}
    names = [entry["name"] for entry in payload["models"]]
    assert names == [
        "last_touch",
        "first_touch",
        "linear",
        "time_decay",
        "position_based",
    ]
    for entry in payload["models"]:
        assert entry["label"]
        assert entry["description"]
        assert entry["favours"]


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "top_level_keys"),
    [
        ("/api/models", {"models"}),
        ("/api/channels?model=last_touch", {"model", "start_date", "end_date", "data"}),
        (
            "/api/model-comparison",
            {
                "models",
                "start_date",
                "end_date",
                "channels",
                "totals_by_model",
                "total_swing",
            },
        ),
        (
            "/api/timeseries?model=linear&granularity=day",
            {"model", "granularity", "start_date", "end_date", "data"},
        ),
        ("/api/journeys?model=linear&limit=5", {"model", "limit", "data"}),
        ("/api/pipeline/health", {"recent_runs", "quarantine", "row_counts"}),
        (
            "/api/summary?model=last_touch",
            {
                "model",
                "total_attributed_revenue",
                "total_spend",
                "blended_roas",
                "conversion_count",
                "touchpoint_count",
                "channel_count",
                "date_range",
                "largest_disagreement",
            },
        ),
    ],
)
def test_every_get_endpoint_returns_expected_shape(client, path, top_level_keys):
    response = client.get(path)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict), "no endpoint may return a bare array"
    assert set(payload) == top_level_keys


def test_channels_rows_have_the_expected_fields(client):
    payload = client.get("/api/channels?model=linear").json()
    assert payload["data"], "expected channel rows"
    row = payload["data"][0]
    assert set(row) == {
        "channel",
        "attributed_revenue",
        "attributed_conversions",
        "spend",
        "roas",
        "touchpoint_count",
    }
    # Sorted by attributed revenue, descending.
    revenues = [entry["attributed_revenue"] for entry in payload["data"]]
    assert revenues == sorted(revenues, reverse=True)


def test_journeys_include_the_full_path_and_credits(client):
    payload = client.get("/api/journeys?model=linear&limit=3").json()
    assert payload["limit"] == 3
    assert payload["data"]
    journey = payload["data"][0]
    assert journey["path"]
    assert journey["touchpoints"]
    assert journey["touchpoint_count"] == len(journey["touchpoints"])
    assert abs(sum(t["credit"] for t in journey["touchpoints"]) - 1.0) < 1e-9


def test_pipeline_health_reports_quarantine_and_counts(client):
    payload = client.get("/api/pipeline/health").json()
    assert payload["row_counts"]["conversions"] > 0
    assert payload["row_counts"]["attribution_results"] > 0
    assert payload["quarantine"]["total"] >= 0
    assert isinstance(payload["quarantine"]["by_source"], dict)
    assert payload["recent_runs"], "ingestion runs should be visible"
    assert payload["recent_runs"][0]["source"]


def test_summary_reports_headline_numbers(client):
    payload = client.get("/api/summary?model=last_touch").json()

    assert payload["model"] == "last_touch"
    assert payload["total_attributed_revenue"] > 0
    assert payload["conversion_count"] > 0
    assert payload["touchpoint_count"] > 0
    assert payload["channel_count"] > 0
    assert payload["date_range"]["start"] is not None
    assert payload["date_range"]["end"] is not None
    # The date range is a real ISO date.
    dt.date.fromisoformat(payload["date_range"]["start"])
    # And it names the biggest model disagreement.
    assert payload["largest_disagreement"]["channel"]
    assert payload["largest_disagreement"]["swing"] > 0


# ---------------------------------------------------------------------------
# The model parameter must actually be wired through
# ---------------------------------------------------------------------------


def test_model_parameter_changes_the_numbers(client):
    """Proves `model` is used rather than quietly ignored."""
    last_touch = client.get("/api/channels?model=last_touch").json()["data"]
    first_touch = client.get("/api/channels?model=first_touch").json()["data"]

    by_channel_last = {row["channel"]: row["attributed_revenue"] for row in last_touch}
    by_channel_first = {row["channel"]: row["attributed_revenue"] for row in first_touch}

    shared = set(by_channel_last) & set(by_channel_first)
    assert shared, "expected overlapping channels"
    differing = [
        channel
        for channel in shared
        if by_channel_last[channel] != by_channel_first[channel]
    ]
    assert differing, (
        "last_touch and first_touch returned identical revenue for every "
        "channel — the model parameter is not wired through"
    )

    # Both still attribute the same total; only the split differs.
    assert abs(sum(by_channel_last.values()) - sum(by_channel_first.values())) < 0.01


def test_model_comparison_exposes_a_nonzero_swing(client):
    payload = client.get("/api/model-comparison").json()
    assert payload["channels"]
    assert payload["total_swing"] > 0
    assert any(row["swing"] > 0 for row in payload["channels"])
    row = payload["channels"][0]
    assert set(row["by_model"]) == {
        "last_touch",
        "first_touch",
        "linear",
        "time_decay",
        "position_based",
    }
    assert row["most_generous_model"] in row["by_model"]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_decimal_fields_are_json_numbers_not_strings(client):
    payload = client.get("/api/channels?model=last_touch").json()
    row = payload["data"][0]

    for field in ("attributed_revenue", "attributed_conversions", "spend"):
        assert isinstance(row[field], (int, float)), (
            f"{field} came back as {type(row[field]).__name__}: {row[field]!r}"
        )
        assert not isinstance(row[field], str)

    summary = client.get("/api/summary").json()
    assert isinstance(summary["total_attributed_revenue"], (int, float))

    comparison = client.get("/api/model-comparison").json()
    assert isinstance(comparison["total_swing"], (int, float))
    first_channel = comparison["channels"][0]
    assert all(
        isinstance(value, (int, float)) for value in first_channel["by_model"].values()
    )

    # And the raw body really has bare numbers, not quoted ones.
    raw = client.get("/api/channels?model=last_touch").text
    assert '"attributed_revenue":"' not in raw


def test_roas_is_null_for_organic_not_an_error(client):
    payload = client.get("/api/channels?model=linear").json()
    rows = {row["channel"]: row for row in payload["data"]}

    assert "organic" in rows, "expected an organic channel in the dataset"
    organic = rows["organic"]
    assert organic["spend"] == 0
    assert organic["roas"] is None, "zero spend must serialise as null"

    # A paid channel does have a ROAS.
    paid = [row for row in payload["data"] if row["spend"] > 0]
    assert paid
    assert all(row["roas"] is not None for row in paid)


def test_timestamps_are_iso_8601(client):
    payload = client.get("/api/journeys?model=linear&limit=1").json()
    journey = payload["data"][0]
    # Parses as ISO 8601, which is the assertion.
    dt.datetime.fromisoformat(journey["converted_at"])
    dt.datetime.fromisoformat(journey["touchpoints"][0]["occurred_at"])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_model_returns_422_listing_the_valid_values(client):
    response = client.get("/api/channels?model=made_up_model")
    assert response.status_code == 422

    body = response.text
    for name in (
        "last_touch",
        "first_touch",
        "linear",
        "time_decay",
        "position_based",
    ):
        assert name in body, f"422 body should name {name}: {body}"


@pytest.mark.parametrize(
    "path",
    [
        "/api/channels?model=linear&start_date=2026-09-01&end_date=2026-08-01",
        "/api/model-comparison?start_date=2026-09-01&end_date=2026-08-01",
        "/api/timeseries?model=linear&start_date=2026-09-01&end_date=2026-08-01",
    ],
)
def test_start_date_after_end_date_returns_422(client, path):
    response = client.get(path)
    assert response.status_code == 422
    assert "start_date" in response.text
    assert "end_date" in response.text


def test_equal_start_and_end_dates_are_allowed(client):
    response = client.get(
        "/api/channels?model=linear&start_date=2026-09-01&end_date=2026-09-01"
    )
    assert response.status_code == 200


def test_limit_above_100_is_rejected(client):
    """Implemented as a hard cap (422), not a silent clamp."""
    response = client.get("/api/journeys?model=linear&limit=101")
    assert response.status_code == 422
    assert "100" in response.text

    assert client.get("/api/journeys?model=linear&limit=100").status_code == 200
    assert client.get("/api/journeys?model=linear&limit=0").status_code == 422


def test_invalid_granularity_returns_422(client):
    response = client.get("/api/timeseries?model=linear&granularity=fortnight")
    assert response.status_code == 422
    assert "day" in response.text


# ---------------------------------------------------------------------------
# Demo reset job
# ---------------------------------------------------------------------------


def test_demo_reset_returns_202_and_reaches_a_terminal_state(client):
    response = client.post(
        "/api/demo/reset",
        json={"seed": 7, "users": 30, "failure_profile": "none"},
    )
    assert response.status_code == 202, response.text
    body = response.json()

    job_id = body["job_id"]
    assert job_id
    assert body["status"] == "running"
    assert body["params"]["seed"] == 7
    assert body["params"]["users"] == 30
    assert body["params"]["failure_profile"] == "none"
    # Locally the cap is high, so nothing is clamped.
    assert body["params"]["users_requested"] == 30
    assert body["params"]["users_capped"] is False
    assert [stage["name"] for stage in body["stages"]] == [
        "reset",
        "ingest",
        "replay",
        "attribute",
    ]

    # TestClient runs background tasks before returning, so by now the job has
    # finished; poll the status endpoint for its terminal state.
    status_response = client.get(f"/api/demo/status/{job_id}")
    assert status_response.status_code == 200
    final = status_response.json()

    assert final["status"] in {"success", "failed"}, final
    assert final["status"] == "success", final.get("error")
    assert final["finished_at"] is not None
    assert final["duration_seconds"] is not None
    assert all(stage["status"] == "success" for stage in final["stages"]), final["stages"]

    ingest_stage = next(s for s in final["stages"] if s["name"] == "ingest")
    assert ingest_stage["detail"]["received"] > 0
    attribute_stage = next(s for s in final["stages"] if s["name"] == "attribute")
    assert attribute_stage["detail"]["rows_written"] > 0


def test_second_demo_reset_while_one_is_running_returns_409(client):
    """The guard is tested against the registry directly.

    TestClient drains background tasks before the response is returned, so a
    real first request would already be finished by the time a second one
    arrives — there would be nothing to conflict with. Registering a running
    job by hand reproduces the in-flight state the guard exists for.
    """
    in_flight = jobs.registry.start(
        {"seed": 1, "users": 10, "failure_profile": "none"}
    )
    assert jobs.registry.active_job_id == in_flight.job_id

    response = client.post(
        "/api/demo/reset",
        json={"seed": 2, "users": 10, "failure_profile": "none"},
    )
    assert response.status_code == 409
    assert in_flight.job_id in response.text

    # Once it finishes, a new run is accepted again.
    jobs.registry.finish(in_flight.job_id)
    assert jobs.registry.active_job_id is None
    accepted = client.post(
        "/api/demo/reset",
        json={"seed": 2, "users": 10, "failure_profile": "none"},
    )
    assert accepted.status_code == 202


def test_demo_status_for_unknown_job_returns_404(client):
    response = client.get("/api/demo/status/does-not-exist")
    assert response.status_code == 404
    assert "unknown job id" in response.text


def test_demo_reset_rejects_an_out_of_range_user_count(client):
    response = client.post(
        "/api/demo/reset", json={"seed": 1, "users": 500_000, "failure_profile": "normal"}
    )
    assert response.status_code == 422

    response = client.post(
        "/api/demo/reset", json={"seed": 1, "users": 10, "failure_profile": "apocalypse"}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_unreachable_database_returns_503_not_500(client):
    """A dead database is a 503 with an actionable message."""

    def broken_db():
        raise OperationalError(
            "SELECT 1", {}, Exception("could not connect to server")
        )

    app.dependency_overrides[get_db] = broken_db
    try:
        response = client.get("/api/channels?model=linear")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "database_unavailable"
    assert "Postgres" in body["detail"]
    assert "Traceback" not in response.text


def test_unhandled_exception_returns_a_clean_500_without_a_traceback():
    """The traceback goes to the log, never into the response body."""

    def exploding_db():
        raise RuntimeError("something entirely unexpected")

    app.dependency_overrides[get_db] = exploding_db
    try:
        with TestClient(app, raise_server_exceptions=False) as quiet_client:
            response = quiet_client.get("/api/summary?model=linear")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "internal_server_error"
    assert "RuntimeError" in body["detail"]
    assert "Traceback" not in response.text
    assert "something entirely unexpected" not in response.text


def test_request_logging_middleware_adds_a_duration_header(client):
    response = client.get("/health")
    assert "X-Response-Time-ms" in response.headers
    assert float(response.headers["X-Response-Time-ms"]) >= 0


def test_openapi_schema_is_valid_and_documented(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()

    assert spec["info"]["title"] == "SignalStack API"
    assert spec["info"]["version"]
    assert "attribution" in spec["info"]["description"].lower()

    for path in (
        "/health",
        "/api/models",
        "/api/channels",
        "/api/model-comparison",
        "/api/timeseries",
        "/api/journeys",
        "/api/pipeline/health",
        "/api/summary",
        "/api/demo/reset",
        "/api/demo/status/{job_id}",
    ):
        assert path in spec["paths"], f"{path} missing from the OpenAPI spec"


def test_docs_page_renders(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower()
