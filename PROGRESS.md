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
