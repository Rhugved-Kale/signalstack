# SignalStack

An ad performance attribution pipeline: it ingests campaign and revenue data, runs
multi-touch attribution, and serves the results to a dashboard.

> Status: Phase 1 — scaffolding only. No pipeline, database, or attribution logic yet.

## Folder structure

```
signalstack/
  backend/
    app/
      __init__.py
      main.py            # FastAPI app + GET /health
      config.py          # pydantic-settings config, exports `settings`
      api/               # (empty) HTTP routers
      db/                # (empty) models, session, migrations wiring
      pipeline/          # (empty) ingestion / transform jobs
      attribution/       # (empty) multi-touch attribution logic
    tests/
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

> **Heads up:** if you already run Postgres locally on port 5432, `localhost:5432`
> will reach *that* server instead of the container. See PROGRESS.md → "Gotchas".

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

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://signalstack:signalstack@localhost:5432/signalstack` | SQLAlchemy connection string |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed browser origins |
| `ENVIRONMENT` | `local` | Environment name, surfaced by `/health` |
