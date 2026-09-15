# SignalStack

An ad performance attribution pipeline: it ingests campaign and revenue data, runs
multi-touch attribution, and serves the results to a dashboard.

> Status: Phase 6 — HTTP API. No frontend dashboard yet.

## Folder structure

```
signalstack/
  backend/
    app/
      __init__.py
      main.py            # FastAPI app + GET /health
      config.py          # pydantic-settings config, exports `settings`
      api/
        routes.py        # endpoints under /api
        schemas.py       # Pydantic v2 response models
        jobs.py          # demo-reset job registry
      db/
        base.py          # declarative Base
        session.py       # engine, SessionLocal, get_db() dependency
        models.py        # ORM models (7 tables)
      generator/
        world.py         # coherent fake world (journeys, spend)
        fake_apis.py     # fake HTTP APIs with realistic failure modes
        cli.py           # inspection CLI
      pipeline/
        schemas.py       # Pydantic v2 validation per source record
        fetcher.py       # pagination + retry policy
        loaders.py       # idempotent batch upserts
        runner.py        # per-source orchestration
        replay.py        # dependency-ordered quarantine replay
        cli.py           # ingestion CLI
      attribution/
        journeys.py      # batched journey assembly
        models.py        # the five attribution models
        engine.py        # scoring runner (idempotent upserts)
        analytics.py     # rollups for the dashboard
        cli.py           # attribution CLI
    alembic/             # migration environment
      versions/          # migration scripts
    alembic.ini
    tests/
      test_db.py         # round-trip test against the real database
      test_generator.py  # generator tests (no database)
      test_pipeline.py   # pipeline tests (real database)
      test_attribution.py # attribution tests
      test_api.py        # API tests (TestClient)
    requirements.txt
    .env.example
  frontend/              # Vite + React (JavaScript)
  docker-compose.yml     # postgres:16
  .gitignore
  README.md
  PROGRESS.md            # running log across phases
```

## Local setup

### 1. Postgres

```bash
docker compose up -d
```

Verify it is healthy:

```bash
docker compose ps
```

The container is published on host port **5433** (not 5432) so it cannot collide
with a Postgres you already run locally. It still listens on 5432 inside the
container.

Then apply the migrations — see [Database](#database).

### 2. Backend

```bash
cd backend
python3.13 -m venv .venv
```

```bash
cd backend && .venv/bin/pip install -r requirements.txt
```

```bash
cd backend && cp .env.example .env
```

```bash
cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000
```

Check it:

```bash
curl http://localhost:8000/health
```

```json
{"status":"ok","service":"signalstack-api","environment":"local"}
```

### 3. Frontend

```bash
cd frontend && npm install
```

```bash
cd frontend && npm run dev
```

The dev server runs on http://localhost:5173 and expects the API at the
`VITE_API_URL` in `frontend/.env` (default `http://localhost:8000`).

Production build:

```bash
cd frontend && npm run build
```

## Database

Postgres 16 runs in Docker, published on host port **5433**. Schema changes are
managed with Alembic; all Alembic commands must be run from `backend/` so that
`backend/.env` is picked up.

### Start Postgres

```bash
docker compose up -d
```

```bash
docker compose ps
```

### Apply migrations

```bash
cd backend && .venv/bin/alembic upgrade head
```

### Other migration commands

Show the currently applied revision:

```bash
cd backend && .venv/bin/alembic current
```

Roll all the way back (drops every application table):

```bash
cd backend && .venv/bin/alembic downgrade base
```

Check whether the models have drifted from the database:

```bash
cd backend && .venv/bin/alembic check
```

Create a new migration after changing `app/db/models.py`:

```bash
cd backend && .venv/bin/alembic revision --autogenerate -m "describe the change"
```

### Connect with psql

```bash
psql "postgresql://signalstack:signalstack@localhost:5433/signalstack"
```

### Tables

| Table | Purpose |
| --- | --- |
| `campaigns` | Campaign dimension, keyed to the ad platform by `external_id` |
| `ad_spend` | Daily spend / impressions / clicks per campaign |
| `touchpoints` | Individual marketing interactions per user |
| `conversions` | Revenue events to be attributed |
| `attribution_results` | Credit per conversion, touchpoint and model |
| `ingestion_runs` | Audit log of ingestion attempts |
| `quarantined_records` | Records that failed validation, kept verbatim |

### Run the database test

Requires Postgres up and migrations applied:

```bash
cd backend && .venv/bin/python -m pytest
```

## Synthetic data generator

`app.generator` builds an internally consistent fake world — real multi-touch
user journeys, not random rows — and serves it through fake HTTP-like APIs that
rate-limit, time out, return 500s, and corrupt records the way real upstreams
do. It never touches the database; it returns plain dicts.

Inspect a world and write sample API payloads:

```bash
cd backend && .venv/bin/python -m app.generator.cli --seed 42 --out samples/
```

The same seed always produces the same world. See PROGRESS.md → "Phase 3" for
the channel/funnel model and the full list of failure modes.

## Ingestion pipeline

Pulls the generated world through the fake APIs and into Postgres: retry on
transport failures, validate every record, quarantine what fails, and upsert
the rest idempotently.

```bash
cd backend && .venv/bin/python -m app.pipeline.cli --reset --users 3000
```

Flags: `--seed`, `--users`, `--failure-profile {none,normal,chaos}`, `--reset`
(TRUNCATE all data tables first), `--quiet`. Running it twice without `--reset`
leaves the data tables unchanged — the upserts are keyed on the natural keys
from the Phase 2 schema.

Replay quarantined records in dependency order — re-fetching parent campaigns
so orphaned children can load:

```bash
cd backend && .venv/bin/python -m app.pipeline.cli --reset --users 3000 --replay
```

```bash
cd backend && .venv/bin/python -m app.pipeline.cli --replay-only
```

See PROGRESS.md → "Phase 4" for the retry policy, what gets quarantined and
why, and the `received == ingested + quarantined` counter identity; "Phase 4.5"
covers replay and the currency allowlist.

## Attribution engine

Scores every conversion under five multi-touch models — `last_touch`,
`first_touch`, `linear`, `time_decay`, `position_based` — and stores all five
side by side so the dashboard can show how much they disagree.

```bash
cd backend && .venv/bin/python -m app.attribution.cli --rebuild
```

Flags: `--rebuild`, `--lookback-days`, `--half-life-days`, `--model`
(repeatable), `--quiet`. Re-running without `--rebuild` is a no-op on row
counts — the upserts are keyed on
`(conversion_id, touchpoint_id, model_name)`.

All credit arithmetic is `Decimal` with largest-remainder apportionment, so
credits sum to exactly `1.000000` and attributed revenue sums to the
conversion's revenue to the cent. See PROGRESS.md → "Phase 5".

> Note: `pytest` truncates the data tables, so re-run the pipeline CLI before
> an attribution demo.

## API

```bash
cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000
```

Interactive docs at http://localhost:8000/docs.

| Method | Path | What it returns |
| --- | --- | --- |
| GET | `/health` | liveness probe |
| GET | `/api/models` | the five models and what each assumes |
| GET | `/api/channels?model=` | revenue, spend and ROAS per channel |
| GET | `/api/model-comparison` | the same revenue under every model, plus swing |
| GET | `/api/timeseries?model=&granularity=` | attributed revenue over time |
| GET | `/api/journeys?model=&limit=` | customer paths with per-touch credit |
| GET | `/api/pipeline/health` | ingestion runs, quarantine, row counts |
| GET | `/api/summary?model=` | headline numbers for the dashboard hero |
| POST | `/api/demo/reset` | rebuild the whole dataset (202 + job id) |
| GET | `/api/demo/status/{job_id}` | poll a demo reset job |

Rebuild the entire dataset from the API — generate, ingest, replay, attribute:

```bash
curl -X POST http://localhost:8000/api/demo/reset -H 'Content-Type: application/json' -d '{"seed":42,"users":3000,"failure_profile":"normal"}'
```

Money serialises as a JSON number; `roas` is `null` (not `0`) for channels with
no spend. See PROGRESS.md → "Phase 6".

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://signalstack:signalstack@localhost:5433/signalstack` | SQLAlchemy connection string |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed browser origins |
| `ENVIRONMENT` | `local` | Environment name, surfaced by `/health` |
