# SignalStack — Progress Log

Running log for the project. Each phase appends a section; do not rewrite history.

## Phase 1 — Scaffolding

Date: 2026-09-14

### What was built

**Repo layout**
- `backend/app/` with `main.py`, `config.py`, and empty packages `api/`, `db/`,
  `pipeline/`, `attribution/` (all with `__init__.py`), plus `backend/tests/`.
- `frontend/` — Vite + React scaffold (JavaScript template, not TypeScript).
- Root: `docker-compose.yml`, `.gitignore`, `README.md`, `PROGRESS.md`.

**Backend**
- venv at `backend/.venv` (Python 3.13.15), created with `python3.13 -m venv`.
- `requirements.txt` pins the versions resolved on 2026-09-14: fastapi 0.141.1,
  uvicorn[standard] 0.53.0, pydantic-settings 2.15.0, python-dotenv 1.2.3,
  sqlalchemy 2.0.53, psycopg[binary] 3.3.5, alembic 1.20.0, tenacity 9.1.4,
  faker 40.39.0, pytest 9.1.1, httpx 0.28.1.
- `app/config.py` — `Settings(BaseSettings)` reading `DATABASE_URL`,
  `CORS_ORIGINS`, `ENVIRONMENT`, with local-dev defaults and `env_file=".env"`.
  Exports a module-level `settings`. Helper property `cors_origins_list` splits
  the comma-separated `CORS_ORIGINS` string.
- `app/main.py` — FastAPI app titled "SignalStack API", CORS middleware from
  `settings.cors_origins_list`, single endpoint `GET /health`.

**Frontend**
- `frontend/.env` with `VITE_API_URL=http://localhost:8000`.
- `src/App.jsx` fetches `${VITE_API_URL}/health` on mount and renders the
  "SignalStack" heading plus a status line: "checking..." / "API: connected"
  (green) / "API: unreachable" (red). Minimal styles in `src/App.css`.
- No router, UI kit, or chart library yet — deliberately.

**Infra**
- `docker-compose.yml`: single `postgres:16` service, user/password/db all
  `signalstack`, host 5432 → container 5432, named volume
  `signalstack-pgdata`, healthcheck via `pg_isready -U signalstack -d signalstack`.

**Verification performed**
- `uvicorn app.main:app --port 8000` → `curl /health` returned HTTP 200 and
  `{"status":"ok","service":"signalstack-api","environment":"local"}`.
- `npm run build` succeeded (vite 8.3.0, 17 modules, ~183ms).
- `docker compose up -d` → container `signalstack-postgres` reached `healthy`
  and was left running.
- `git init` + single commit "Phase 1: project scaffolding".

### Notes / gotchas for future sessions

1. **`python3` on this machine is 3.9.6 (Apple system Python, EOL Oct 2025).**
   The venv was built with `python3.13` instead. Use
   `backend/.venv/bin/python` (or `python3.13`) — not bare `python3` — or the
   pinned dependency versions will not install.

2. **Port 5432 is contested — this will bite when DB code lands in Phase 2.**
   A Homebrew `postgresql@16` service is running and bound to the *specific*
   loopback addresses (`127.0.0.1:5432` and `[::1]:5432`). The Docker container
   binds the *wildcard* (`*:5432`). On macOS the more specific bind wins, so:
   - `localhost:5432` → **Homebrew** Postgres (no `signalstack` role; connecting
     with the default `DATABASE_URL` fails with
     `FATAL: role "signalstack" does not exist`).
   - The container is reachable via `docker exec` or the host's LAN IP.

   Both servers are up right now. Pick one before writing DB code:
   - *Preferred* — stop the Homebrew service: `brew services stop postgresql@16`,
     then `localhost:5432` reaches the container and the default `DATABASE_URL`
     works as written.
   - Or remap the container to `5433:5432` in `docker-compose.yml` and set
     `DATABASE_URL=...@localhost:5433/signalstack` in `backend/.env`.

3. **`frontend/.env` is intentionally untracked** — the root `.gitignore`
   ignores `.env` (while keeping `.env.example`). A fresh clone must recreate
   `frontend/.env` with `VITE_API_URL`. Consider adding a
   `frontend/.env.example` in a later phase.

4. **No `backend/.env` exists yet** — `config.py` defaults cover local dev, so
   the API boots without one. Copy `.env.example` → `.env` when you need to
   override anything.

5. Docker Desktop was not running at the start of this session; it had to be
   launched before `docker compose` would work.

6. `pytest` and `httpx` are installed but **no tests are written yet** —
   `backend/tests/` contains only `__init__.py`.

### Not done (by design — scaffolding only)
No database models, migrations (alembic is installed but not initialized —
there is no `alembic.ini` or `versions/` tree yet), pipeline code, attribution
logic, additional API endpoints, or deployment.

## Phase 2 — Database schema

Date: 2026-09-14

Scope: schema and migrations only. No ingestion, attribution, API endpoints, or
seed data — those remain for later phases.

### Port change: 5433 (resolves the Phase 1 gotcha)

Phase 1 note #2 documented a collision on port 5432: a Homebrew
`postgresql@16` service binds the specific loopback addresses, the container
bound the wildcard, and the specific bind wins — so `localhost:5432` reached
Homebrew Postgres, not our container.

Resolved by moving the container's **host** port to 5433 (`"5433:5432"` in
`docker-compose.yml`); the container still listens on 5432 internally. The
Homebrew service was left running and untouched, so port 5432 still belongs to
it. The new port is reflected in three places:

- `docker-compose.yml` → `ports: ["5433:5432"]`
- `backend/app/config.py` → `DATABASE_URL` default
- `backend/.env.example`, and a new **gitignored** `backend/.env`

Proof that 5433 reaches the container: `SELECT version()` on 5433 returns
`PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) ... aarch64-unknown-linux-gnu` with
`inet_server_addr() = 172.19.0.2`, whereas 5432 still returns
`PostgreSQL 16.13 (Homebrew) ... aarch64-apple-darwin`.

### Tables

Seven application tables (plus Alembic's own `alembic_version`):

| Table | Purpose |
| --- | --- |
| `campaigns` | Campaign dimension, keyed to the upstream platform by `external_id`. Parent of ad spend and touchpoints. |
| `ad_spend` | Daily cost facts per campaign (spend/impressions/clicks) — the denominator for ROAS. |
| `touchpoints` | Individual marketing interactions per `user_id`; the journey that attribution walks. `campaign_id` is nullable so organic/direct touches can exist. |
| `conversions` | Revenue events to be attributed, keyed by the payment API's order id. |
| `attribution_results` | Credit per (conversion, touchpoint, model). Holds every model's answer side by side rather than overwriting. |
| `ingestion_runs` | One row per ingestion attempt: counts, status, retry count, error. The pipeline's audit log. |
| `quarantined_records` | Records that failed validation, with the original payload kept verbatim in `JSONB` for replay. |

Conventions: SQLAlchemy 2.0 `Mapped`/`mapped_column`; integer surrogate `id` on
every table; all timestamps `TIMESTAMP WITH TIME ZONE`; all money
`NUMERIC(12,2)` and `credit` `NUMERIC(8,6)` — no floats anywhere.

Idempotency is enforced in the schema, not in code: `ad_spend` is unique on
`(campaign_id, date)`, `attribution_results` on
`(conversion_id, touchpoint_id, model_name)`, and `external_id` is unique on
`campaigns`, `touchpoints`, and `conversions`. Re-ingesting the same data should
therefore conflict rather than duplicate.

### Files added

- `backend/app/db/base.py` — declarative `Base`, isolated so models and
  Alembic's `env.py` can both import it without a cycle.
- `backend/app/db/session.py` — `engine` (with `pool_pre_ping=True`),
  `SessionLocal`, and the `get_db()` FastAPI dependency.
- `backend/app/db/models.py` — the seven models.
- `backend/alembic.ini`, `backend/alembic/` — migration environment.
- `backend/alembic/versions/bc011872e3ef_initial_schema.py` — initial schema.
- `backend/tests/test_db.py` — real-database round-trip test.

### Running migrations

All Alembic commands run from `backend/`:

```bash
cd backend && .venv/bin/alembic upgrade head      # apply
cd backend && .venv/bin/alembic downgrade base    # roll all the way back
cd backend && .venv/bin/alembic current           # show applied revision
cd backend && .venv/bin/alembic check             # detect model/DB drift
cd backend && .venv/bin/alembic revision --autogenerate -m "message"
```

### Verification performed

- Autogenerate produced all 7 tables, 5 foreign keys, 5 unique constraints and
  10 indexes on the first pass — **no hand-editing of the migration was
  needed**. Confirmed by reading the file and by `alembic check` reporting
  "No new upgrade operations detected".
- `\dt` shows 8 tables; `alembic_version` holds `bc011872e3ef`.
- Reversibility: `downgrade base` left 0 application tables, then
  `upgrade head` restored all 7, with `alembic check` still clean afterwards.
- `pytest` — 1 passed, against the real Postgres, leaving no residual rows.

### Notes / gotchas for future sessions

1. **`alembic/env.py` ignores `alembic.ini` for the URL.** It imports
   `app.config.settings` and calls `config.set_main_option("sqlalchemy.url", ...)`,
   so `DATABASE_URL` has exactly one source of truth. The `sqlalchemy.url` key in
   `alembic.ini` is deliberately left blank — do not put credentials there.

2. **Run Alembic and pytest from `backend/`.** `config.py` sets
   `env_file=".env"`, which resolves relative to the *current working
   directory*, so `backend/.env` is only picked up from there. (Defaults in
   `config.py` also point at 5433, so a wrong cwd happens to still work today —
   it will stop being harmless once `.env` diverges from the defaults.)

3. **`env.py` inserts `backend/` into `sys.path`** so the `app` package imports
   regardless of how Alembic is invoked.

4. **`tests/__init__.py` is what makes `from app...` work under pytest** — it
   makes `tests` a package, so pytest inserts `backend/` (not `backend/tests/`)
   onto `sys.path`. Don't delete it.

5. **The single-column unique constraints are DB-named**, e.g.
   `campaigns_external_id_key`, because they come from `unique=True` on the
   column rather than a named `UniqueConstraint`. The composite ones are
   explicitly named (`uq_ad_spend_campaign_date`,
   `uq_attribution_conversion_touchpoint_model`). If you later want fully
   predictable names everywhere, add a naming convention to `Base.metadata` —
   that will require a migration to rename.

6. **No ORM `relationship()` attributes were defined** — only the raw foreign
   keys the spec called for. Add them in the phase that needs to traverse
   journeys (attribution will want `Conversion` → `Touchpoint`), and remember
   relationships need no migration.

7. **No `ON DELETE` behaviour was specified**, so foreign keys use Postgres's
   default `NO ACTION`. Deleting a campaign with spend or touchpoints attached
   will therefore raise. Decide on cascade rules when deletion becomes a real
   workflow.

8. **The `ad_spend.date` column shadows the `date` type name.** `models.py`
   imports `datetime as dt` and annotates `Mapped[dt.date]` to avoid the
   collision — keep that style if you add more date columns.

9. Docker Desktop still has to be running before any of this works; the
   container is `signalstack-postgres`.
