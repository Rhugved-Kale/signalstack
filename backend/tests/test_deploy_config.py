"""Tests for the production configuration added in Phase 8.

These cover the things that only break once the app is deployed: a connection
string in a provider's format, a real https CORS origin, the demo cap on a
small instance, and the self-bootstrap.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.bootstrap import BootstrapStatus, database_is_empty, run_bootstrap
from app.config import (
    HOSTED_MAX_DEMO_USERS,
    LOCAL_MAX_DEMO_USERS,
    Settings,
    normalize_database_url,
)
from app.db.models import AttributionResult, Campaign
from app.db.session import SessionLocal
from app.observability import peak_rss_mb
from app.pipeline.cli import DATA_TABLES

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent


# ---------------------------------------------------------------------------
# DATABASE_URL normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # Neon hands out postgresql:// with a required sslmode.
        (
            "postgresql://u:p@ep-x.us-west-2.aws.neon.tech/neondb?sslmode=require",
            "postgresql+psycopg://u:p@ep-x.us-west-2.aws.neon.tech/neondb?sslmode=require",
        ),
        # Heroku-style legacy scheme.
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        # Already correct — must not be double-prefixed.
        (
            "postgresql+psycopg://u:p@localhost:5433/signalstack",
            "postgresql+psycopg://u:p@localhost:5433/signalstack",
        ),
        # Multiple query params survive intact.
        (
            "postgresql://u:p@host/db?sslmode=require&channel_binding=require",
            "postgresql+psycopg://u:p@host/db?sslmode=require&channel_binding=require",
        ),
        # Surrounding whitespace from a copy-paste is trimmed.
        ("  postgres://u:p@host/db  ", "postgresql+psycopg://u:p@host/db"),
    ],
)
def test_database_url_is_normalised_onto_psycopg(given, expected):
    assert normalize_database_url(given) == expected


def test_non_postgres_urls_are_left_alone():
    """Better to pass an unknown scheme through than to mangle it."""
    assert normalize_database_url("sqlite:///./local.db") == "sqlite:///./local.db"


def test_settings_normalises_on_load(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://u:p@ep-x.neon.tech/neondb?sslmode=require"
    )
    loaded = Settings(_env_file=None)
    assert loaded.DATABASE_URL.startswith("postgresql+psycopg://")
    assert loaded.DATABASE_URL.endswith("?sslmode=require")


def test_sqlalchemy_accepts_the_normalised_neon_url():
    """Parse-only: proves the URL resolves to the installed psycopg3 driver."""
    from sqlalchemy.engine import make_url

    url = make_url(
        normalize_database_url(
            "postgresql://u:p@ep-x.us-west-2.aws.neon.tech/neondb?sslmode=require"
        )
    )
    assert url.drivername == "postgresql+psycopg"
    assert url.query.get("sslmode") == "require"
    assert url.host.endswith("neon.tech")
    # The driver the URL names is actually installed.
    assert url.get_dialect().driver == "psycopg"


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_cors_origins_accepts_a_comma_separated_https_list(monkeypatch):
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "https://signalstack.vercel.app, http://localhost:5173 ,",
    )
    loaded = Settings(_env_file=None)
    assert loaded.cors_origins_list == [
        "https://signalstack.vercel.app",
        "http://localhost:5173",
    ]


def test_cors_origins_single_https_value(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://signalstack.vercel.app")
    assert Settings(_env_file=None).cors_origins_list == [
        "https://signalstack.vercel.app"
    ]


def test_https_origin_is_echoed_back_by_the_middleware(monkeypatch):
    """End-to-end: a real https origin gets an allow-origin header."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CORS_ORIGINS", "https://signalstack.vercel.app")
    loaded = Settings(_env_file=None)

    probe = FastAPI()
    probe.add_middleware(
        CORSMiddleware,
        allow_origins=loaded.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @probe.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(probe) as probe_client:
        allowed = probe_client.get(
            "/ping", headers={"Origin": "https://signalstack.vercel.app"}
        )
        assert (
            allowed.headers["access-control-allow-origin"]
            == "https://signalstack.vercel.app"
        )

        denied = probe_client.get("/ping", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in denied.headers


# ---------------------------------------------------------------------------
# Demo user cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "expected_cap"),
    [
        ("local", LOCAL_MAX_DEMO_USERS),
        ("production", HOSTED_MAX_DEMO_USERS),
        ("staging", HOSTED_MAX_DEMO_USERS),
        ("LOCAL", LOCAL_MAX_DEMO_USERS),
    ],
)
def test_demo_cap_depends_on_environment(monkeypatch, environment, expected_cap):
    monkeypatch.setenv("ENVIRONMENT", environment)
    assert Settings(_env_file=None).max_demo_users == expected_cap


def test_hosted_cap_is_1500():
    assert HOSTED_MAX_DEMO_USERS == 1500


def test_demo_reset_clamps_users_when_not_local(monkeypatch):
    """A 3000-user request on a small instance generates 1500 and says so."""
    from app.api import jobs, routes
    from app.api.schemas import DemoResetRequest, FailureProfile
    from fastapi import BackgroundTasks

    monkeypatch.setattr(routes.settings, "ENVIRONMENT", "production")
    jobs.registry.reset()
    try:
        tasks = BackgroundTasks()
        response = routes.post_demo_reset(
            DemoResetRequest(
                seed=42, users=3000, failure_profile=FailureProfile.none
            ),
            tasks,
        )
        assert response.params["users"] == HOSTED_MAX_DEMO_USERS
        assert response.params["users_requested"] == 3000
        assert response.params["users_capped"] is True
        assert response.params["max_users"] == HOSTED_MAX_DEMO_USERS
        # The background task was queued with the clamped value.
        assert tasks.tasks
        assert tasks.tasks[0].kwargs.get("params", tasks.tasks[0].args[1])["users"] == (
            HOSTED_MAX_DEMO_USERS
        )
    finally:
        jobs.registry.reset()


def test_demo_reset_does_not_clamp_below_the_cap(monkeypatch):
    from app.api import jobs, routes
    from app.api.schemas import DemoResetRequest, FailureProfile
    from fastapi import BackgroundTasks

    monkeypatch.setattr(routes.settings, "ENVIRONMENT", "production")
    jobs.registry.reset()
    try:
        response = routes.post_demo_reset(
            DemoResetRequest(seed=1, users=400, failure_profile=FailureProfile.none),
            BackgroundTasks(),
        )
        assert response.params["users"] == 400
        assert response.params["users_capped"] is False
    finally:
        jobs.registry.reset()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_db():
    session = SessionLocal()
    session.rollback()
    session.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(
            text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE")
        )
        session.commit()
        session.close()


def test_database_is_empty_detects_both_states(empty_db):
    assert database_is_empty(empty_db) is True
    empty_db.add(
        Campaign(
            external_id="cmp_probe",
            name="Probe",
            channel="display",
            platform="meta_ads",
        )
    )
    empty_db.commit()
    assert database_is_empty(empty_db) is False


def test_bootstrap_populates_an_empty_database(empty_db):
    """A fresh deployment must never show an empty dashboard."""
    assert database_is_empty(empty_db)

    state = run_bootstrap(seed=42, users=60)
    assert state == "complete"

    empty_db.expire_all()
    assert empty_db.scalar(select(func.count()).select_from(Campaign)) > 0
    assert empty_db.scalar(select(func.count()).select_from(AttributionResult)) > 0


def test_bootstrap_is_idempotent_and_skips_when_data_exists(empty_db):
    """A restart or redeploy must not wipe or duplicate anything."""
    run_bootstrap(seed=42, users=60)
    empty_db.expire_all()
    campaigns_before = empty_db.scalar(select(func.count()).select_from(Campaign))
    rows_before = empty_db.scalar(select(func.count()).select_from(AttributionResult))

    assert run_bootstrap(seed=42, users=60) == "skipped"

    empty_db.expire_all()
    assert empty_db.scalar(select(func.count()).select_from(Campaign)) == campaigns_before
    assert (
        empty_db.scalar(select(func.count()).select_from(AttributionResult))
        == rows_before
    )


def test_bootstrap_status_transitions_are_thread_safe():
    tracker = BootstrapStatus()
    assert tracker.state == "pending"
    tracker.set("running", "generating")
    assert tracker.state == "running"
    assert tracker.detail == "generating"
    tracker.set("complete")
    assert tracker.state == "complete"
    assert tracker.detail is None


# ---------------------------------------------------------------------------
# Deployment artefacts
# ---------------------------------------------------------------------------


def test_start_script_is_executable_and_fails_closed():
    script = BACKEND_DIR / "start.sh"
    assert script.exists()
    assert script.stat().st_mode & stat.S_IXUSR, "start.sh must be executable"

    body = script.read_text()
    assert "set -euo pipefail" in body
    assert "alembic upgrade head" in body
    # A failed migration must abort rather than boot a broken server.
    assert "exit 1" in body
    # Render injects PORT; 8000 locally.
    assert '${PORT:-8000}' in body
    assert "uvicorn app.main:app" in body


def test_render_yaml_describes_the_service():
    import yaml

    spec = yaml.safe_load((REPO_ROOT / "render.yaml").read_text())
    service = spec["services"][0]

    assert service["rootDir"] == "backend"
    assert service["startCommand"] == "./start.sh"
    assert service["healthCheckPath"] == "/health"
    assert "pip install -r requirements.txt" in service["buildCommand"]

    env_vars = {entry["key"]: entry for entry in service["envVars"]}
    for required in ("DATABASE_URL", "CORS_ORIGINS", "ENVIRONMENT"):
        assert required in env_vars, f"{required} missing from render.yaml"
    # Secrets must be prompted for, never committed.
    assert env_vars["DATABASE_URL"].get("sync") is False
    assert env_vars["CORS_ORIGINS"].get("sync") is False


def test_requirements_are_all_exactly_pinned():
    lines = [
        line.strip()
        for line in (BACKEND_DIR / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "requirements.txt should not be empty"
    for line in lines:
        assert "==" in line, f"unpinned requirement: {line}"
        for loose in (">=", "<=", "~=", ">", "<", "*"):
            assert loose not in line, f"non-exact pin: {line}"


def test_frontend_deploy_files_exist():
    import json

    frontend = REPO_ROOT / "frontend"
    vercel = json.loads((frontend / "vercel.json").read_text())
    # Any path must serve index.html for a client-rendered single page.
    assert vercel["rewrites"][0]["destination"] == "/index.html"
    assert (frontend / ".env.example").exists()
    assert "VITE_API_URL" in (frontend / ".env.example").read_text()


def test_no_hardcoded_api_url_in_frontend_source():
    """VITE_API_URL is the only way the frontend learns the API's location.

    This looks for URL *literals* (a scheme plus a host), not bare hostnames:
    `api.js` legitimately compares against a set of local hostnames to decide
    whether to show shell commands on the error screen, which is not a
    hardcoded endpoint.
    """
    import re

    src = REPO_ROOT / "frontend" / "src"
    # A scheme-qualified local address, or either deployment host.
    forbidden = re.compile(
        r"https?://(?:localhost|127\.\d|0\.0\.0\.0|\[?::1\]?)"
        r"|[A-Za-z0-9-]+\.onrender\.com"
        r"|[A-Za-z0-9-]+\.vercel\.app",
        re.IGNORECASE,
    )
    offenders = []
    for path in src.rglob("*.js*"):
        for match in forbidden.finditer(path.read_text()):
            offenders.append(f"{path.relative_to(src)}: {match.group(0)}")
    assert not offenders, f"hardcoded API URL in frontend source: {offenders}"


def test_error_screen_gates_local_instructions_on_a_local_api():
    """Shell commands must not be shown to someone on the public URL."""
    src = REPO_ROOT / "frontend" / "src"
    api_js = (src / "api.js").read_text()
    wakeup = (src / "components" / "ApiWakeup.jsx").read_text()

    # The predicate exists and is exported.
    assert "export const IS_LOCAL_API" in api_js
    # The error screen branches on it, and the shell advice sits on the local
    # branch while a free-hosting explanation sits on the other.
    assert "IS_LOCAL_API ?" in wakeup
    assert "./start.sh" in wakeup
    assert "free hosting" in wakeup


def test_peak_rss_is_reported_in_megabytes():
    value = peak_rss_mb()
    # Anything outside this range means the bytes/KB unit conversion is wrong.
    assert 10 < value < 4096, f"implausible peak RSS reading: {value}"
