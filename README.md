# SignalStack

An ad performance attribution pipeline: it ingests campaign and revenue data, runs
multi-touch attribution, and serves the results to a dashboard.

> Status: Phase 2 — database schema and migrations. No ingestion, attribution,
> or dashboard logic yet.

## Folder structure

```
signalstack/
  backend/
    app/
      __init__.py
      main.py            # FastAPI app + GET /health
      config.py          # pydantic-settings config, exports `settings`
      api/               # (empty) HTTP routers
      db/
        base.py          # declarative Base
        session.py       # engine, SessionLocal, get_db() dependency
        models.py        # ORM models (7 tables)
      pipeline/          # (empty) ingestion / transform jobs
      attribution/       # (empty) multi-touch attribution logic
    alembic/             # migration environment
      versions/          # migration scripts
    alembic.ini
    tests/
      test_db.py         # round-trip test against the real database
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

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://signalstack:signalstack@localhost:5433/signalstack` | SQLAlchemy connection string |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed browser origins |
| `ENVIRONMENT` | `local` | Environment name, surfaced by `/health` |
