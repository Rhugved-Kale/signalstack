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

## Phase 3 — Synthetic data generator

Date: 2026-09-14

Scope: the generator only. It builds a fake world and serves it through fake
HTTP-like APIs as plain dicts. It does **not** touch the database, does not
import `app.db` (verified — importing `app.generator` loads no SQLAlchemy,
psycopg, `app.db` or `app.config` module), and contains no ingestion logic.
Phase 4 consumes it.

### Files added

- `backend/app/generator/world.py` — the coherent world.
- `backend/app/generator/fake_apis.py` — three fake APIs with failure modes.
- `backend/app/generator/cli.py` — inspection CLI.
- `backend/tests/test_generator.py` — 21 tests, no database.

### The world model

`build_world(WorldConfig(...)) -> World`. Everything derives from one seeded
`random.Random` plus a Faker seeded identically via `seed_instance`, so a seed
reproduces a byte-identical world. Defaults: seed 42, 12 campaigns, 800 users,
a 60-day window ending today, 22% conversion rate.

`World` holds `campaigns`, `users`, `touchpoints`, `conversions` and
`daily_spend` as plain dicts, plus `summary()`, `channel_breakdown()` and
`touchpoints_before_conversion()` helpers used by the CLI.

Generation order matters: campaigns → users → journeys (touchpoints +
conversions) → daily spend. Spend is generated **last and from the journeys**,
which is what keeps it from contradicting them.

### Funnel bias (the part that makes attribution meaningful)

Each channel has a `funnel_score` on a 0.0 (first touch) .. 1.0 (last touch)
scale, and a channel is picked per touchpoint by weighting
`volume_weight * gaussian(funnel_score - position)` with `FUNNEL_SIGMA = 0.25`.
Organic has `funnel_score = None`, meaning a flat distribution — it can appear
anywhere.

| Channel | Platform | Funnel | Cost model | Touch types |
| --- | --- | --- | --- | --- |
| `display` | `meta_ads` | early (0.10) | CPM $4.50 | impression 92%, click 8% |
| `social` | `meta_ads` | early/mid (0.30) | CPM $8.00 | impression 60%, click 40% |
| `affiliate` | `impact` | mid (0.50) | CPC $0.85 | click |
| `email` | `klaviyo` | late (0.80) | near-zero | email_open 70%, click 30% |
| `paid_search` | `google_ads` | late (0.90) | CPC $2.40 | click |
| `organic` | `organic` | any | none (zero) | visit 65%, click 35% |

With seed 42 the bias is stark: display opens 350 journeys and closes 2;
paid_search opens 0 and closes 237. A test asserts both directions.

Converting users skew longer (weights favour 3-5 touchpoints) than
non-converters (weights favour 1-2). With seed 42, 67.6% of conversions have
3+ prior touchpoints, so multi-touch models will have something to disagree
about.

### Coherence guarantees (all test-enforced)

- A conversion always lands 1-2 days after the user's **last** touchpoint, so
  it can never precede their first.
- Reported `impressions`/`clicks` for a campaign-day are always >= the
  touchpoints actually generated for it, and `impressions >= clicks` always.
  The touchpoint stream is treated as a *tracked sample* of real activity.
- Organic touchpoints have `campaign_id = None`; organic campaigns have zero
  spend, impressions and clicks.
- Timestamps within a journey are strictly increasing and timezone-aware UTC.
- Revenue is lognormal (median ~$140), clamped to $25-$900, 2 decimal places.
- Weekends dip ~20% (measured 19.8% with seed 42).

### Fake APIs and failure modes

Three classes, each `(world, failure_config, seed)`:

| Class | Methods |
| --- | --- |
| `FakeAdPlatformAPI` | `list_campaigns`, `list_ad_spend` |
| `FakeEventStreamAPI` | `list_touchpoints` |
| `FakePaymentAPI` | `list_conversions` |

Every method rolls the failure dice **first**, then returns the envelope
`{data, page, per_page, total, total_pages, has_more}`.

Exceptions: `FakeAPIError` (base, `.status_code` + `.message`),
`RateLimitError` (429, `.retry_after`), `ServerError` (500 or 503),
`FakeTimeoutError`. `FailureConfig` defaults: 8% server error, 5% rate limit,
3% timeout, 7% malformed records, 2% duplicates. `FailureConfig.none()`
returns all-zero rates for deterministic tests.

Nine malformed variants are registered through a `@corruptor(name, requires)`
decorator into a `CORRUPTORS` list — **add a new one by writing a function**,
no other code changes. `requires` names the `RecordShape` field that must be
non-empty for the variant to apply, so e.g. `unexpected_currency` only fires on
the payment API: missing required key, numeric-as-string, numeric-null,
timestamp garbage, timestamp-as-unix-int, negative money, unexpected currency,
empty id, extra unexpected field.

### Running the CLI

```bash
cd backend && .venv/bin/python -m app.generator.cli --seed 42 --out samples/
```

Prints the summary, a channel table and the touchpoints-before-conversion
distribution, and writes sample JSON pages. Also accepts `--campaigns`,
`--users` and `--days`. `samples/` is gitignored (regenerable output).

```bash
cd backend && .venv/bin/python -m pytest tests/test_generator.py -q
```

### Notes / gotchas for future sessions

1. **Money is `float` in these payloads, on purpose.** They imitate JSON, and
   real JSON APIs send numbers. Phase 4 must convert with
   `Decimal(str(value))` — never `Decimal(float)` — before touching the
   `NUMERIC(12,2)` columns.

2. **`campaign_id = None` is legitimate, `campaign_id = ""` is corruption.**
   Organic touchpoints genuinely have no campaign; the `empty_id` corruptor
   emits an empty string. Ingestion must treat these differently — null is
   valid, empty string is quarantine-worthy.

3. **Duplicates make `len(data)` exceed `per_page`.** When
   `duplicate_record_rate > 0` a page can return more rows than requested,
   which is why the pagination test uses `FailureConfig.none()`. Real dedupe
   is the schema's job (the `external_id` unique constraints from Phase 2).

4. **`_roll_failure()` always consumes exactly three RNG draws**, whichever
   failure fires. That is deliberate: it keeps the random stream aligned so a
   seed reproduces the same sequence of failures. If you add a fourth failure
   type, keep the draws unconditional.

5. **An extra unexpected field must not be fatal in Phase 4.** The
   `extra_unexpected_field` corruptor exists specifically to test that
   ingestion ignores unknown keys rather than quarantining the record.

6. **Timestamp corruption raises `ValueError`, not just bad values.**
   `datetime.fromisoformat("0000-00-00")` raises `ValueError: year 0 is out of
   range`. The parser in Phase 4 needs a `try/except ValueError`, not only a
   format check — this bit the test helper first time round.

7. **`total` / `total_pages` are computed before corruption**, from the clean
   record count, so they describe the underlying data rather than the
   corrupted page.

8. **Invalid `page`/`per_page` raise `ValueError`, not `FakeAPIError`** — that
   is a caller bug, not an upstream failure. It is raised *after* the failure
   dice, because the spec requires the dice to roll first.

9. **Spend is sized to the world, not to a real ad account.** 800 users
   produce ~2,200 touchpoints, so per-campaign volumes are small (a display
   campaign spends ~$18/day). Blended ROAS lands at 2.87 with seed 42. Raising
   `n_users` is the honest way to scale volume up; the `daily_impression_base`
   / `daily_click_base` fields on `ChannelSpec` tune cost per channel.

10. **An organic *campaign* exists even though organic touchpoints carry no
    campaign_id.** The spec asked for both, so the organic campaign row is
    real but always has zero spend and zero events. Harmless, but do not treat
    it as a bug when reconciling.

## Phase 4 — Ingestion pipeline

Date: 2026-09-14

Scope: ingestion only — fetch, validate, route, load. No attribution logic, no
API endpoints, no frontend.

### Files added

- `backend/app/pipeline/schemas.py` — Pydantic v2 validation per source record.
- `backend/app/pipeline/fetcher.py` — pagination + tenacity retry policy.
- `backend/app/pipeline/loaders.py` — idempotent batch upserts.
- `backend/app/pipeline/runner.py` — per-source orchestration.
- `backend/app/pipeline/cli.py` — `python -m app.pipeline.cli`.
- `backend/tests/test_pipeline.py` — 41 tests.

### The flow

For each source, in this order (**campaigns must be first** — everything else
resolves campaigns by `external_id`, so ingesting them first would quarantine
the lot):

1. **Open** an `IngestionRun` with status `running` and commit it immediately,
   so a crashed process still leaves the attempt on record.
2. **Fetch** every page through `fetch_all_pages`, retrying each page
   independently.
3. **Validate** each record as it streams past. Valid records accumulate;
   invalid ones go to `quarantined_records` with the raw payload and a
   one-line reason. A rejection costs exactly that one record.
4. **Load** the survivors with a batched upsert. Records whose campaign cannot
   be resolved come back from the loader and are quarantined too.
5. **Close** the run: `finished_at`, `retry_count`, the three counters, and a
   status of `success` (nothing quarantined), `partial` (some were) or
   `failed` (the source blew up).

Each source commits on its own, so a late failure cannot undo earlier work.

### Retry policy

| Failure | Behaviour |
| --- | --- |
| `ServerError` (500/503) | retry, exponential backoff + jitter |
| `FakeTimeoutError` | retry, exponential backoff + jitter |
| `RateLimitError` (429) | retry, waiting exactly `retry_after` seconds |
| anything else (e.g. `ValidationError`) | **not retried** — reproducing a data problem is pointless |

5 attempts max, backoff from 0.5s capped at 8s. On exhaustion the fetcher
raises `PipelineFetchError` carrying the last exception. Retries are counted in
a `FetchStats` object the caller passes in (a generator cannot return a value)
and land on `IngestionRun.retry_count`. `sleep_fn` is injectable so tests run
instantly.

### What gets quarantined, and why

Validation rejects: nulls, empty strings and unparseable values in required
fields; negative spend or revenue; `clicks > impressions`; a currency that is
not three alphabetic letters; unparseable timestamps. Tolerated as ordinary API
sloppiness: numeric strings (`"42.50"`), naive timestamps (assumed UTC), Unix
epoch timestamps, and unknown extra fields (ignored, never fatal).

Referential rejects: a touchpoint or ad-spend row naming a campaign that does
not exist. These are quarantined rather than nulled — nulling a touchpoint's
`campaign_id` would silently relabel paid traffic as organic and corrupt every
attribution result downstream. An *explicitly null* `campaign_id` is fine
(organic); a **missing** `campaign_id` key is rejected, because a dropped field
is indistinguishable from real absence.

### Running the CLI

```bash
cd backend && .venv/bin/python -m app.pipeline.cli --reset --users 3000
```

Flags: `--seed` (42), `--users` (3000), `--failure-profile {none,normal,chaos}`
(normal), `--reset` (TRUNCATE all data tables RESTART IDENTITY CASCADE), and
`--quiet`. Prints a per-source table plus the top 5 quarantine reasons.

```bash
cd backend && .venv/bin/python -m pytest tests/test_pipeline.py -q
```

### Counter identity

    records_received == records_ingested + records_quarantined

Exact, and test-enforced per source and in total. `records_ingested` counts
unique rows upserted **plus in-batch duplicates that were dropped**, because a
dropped duplicate's data *is* in the database — its twin put it there.
`IngestionRun` has no column for the breakdown, so `rows_inserted`,
`rows_updated` and `duplicates_dropped` are returned in the result dict
instead.

### Verification performed

Two consecutive runs at `--users 3000`, the second without `--reset`:
identical `received/ingested/quarantined` (9,862 / 8,938 / 924) both times, but
`rows_inserted` went 8,754 → **0** and `rows_updated` 0 → **8,754**. Row counts
were byte-identical before and after (campaigns 11, ad_spend 633, touchpoints
7,417, conversions 693) and every duplicate-key check returned 0.

### Notes / gotchas for future sessions

1. **One corrupted campaign cascades hard.** In the 3,000-user run a single
   malformed campaign record (1 of 12) was quarantined, and because that
   campaign never loaded, **462 touchpoints and 58 ad_spend rows** referencing
   it were quarantined as "unknown campaign_id" — 56% of all quarantine volume
   traced to one bad parent row. This is correct behaviour, not a bug, but it
   means the highest-value recovery action is replaying quarantined *campaign*
   records first. A future phase should re-drive quarantine in dependency
   order.

2. **In-batch dedupe is mandatory, not an optimisation.** Postgres raises
   "ON CONFLICT DO UPDATE command cannot affect row a second time" if one
   statement touches the same conflict key twice, and the generator emits
   duplicates by design. `loaders._dedupe` collapses them last-wins (matching
   upsert semantics) before the statement runs.

3. **Inserted vs updated comes from Postgres's `xmax`.** `RETURNING xmax = 0`
   is true for a row the statement inserted, false for one it updated. Keeps
   it to one round trip per batch instead of a pre-flight SELECT.

4. **`ingested_at` is refreshed on conflict**, so re-ingesting bumps it. Row
   counts stay identical but `ingested_at` moves — that is deliberate
   (last-seen time), so do not treat it as an idempotency violation.

5. **`ingestion_runs` and `quarantined_records` are append-only audit tables.**
   They grow by one set per run (4 → 8 runs, 924 → 1,848 quarantine rows across
   the two demo runs). Only the four data tables are idempotent.

6. **A failed fetch still loads what it already collected.** `_ingest_source`
   catches the fetch exception and *then* runs the loader on whatever was
   accumulated. This both avoids throwing away good data already paid for and
   is what keeps the counter identity exact when a page dies mid-source.

7. **`run_ingestion` takes two arguments beyond the specified signature**:
   `world_config` (so the CLI can pass `--users` through to the generator,
   which `world_seed` alone cannot express) and `sleep_fn` (so tests skip
   backoff). Both are keyword-only with safe defaults.

8. **`chaos` can kill a source outright.** Per-attempt failure probability is
   ~41%, so 5 consecutive failures on a single page happens roughly 1% of the
   time; with seed 42 it killed `campaigns` entirely in one trial, after which
   every child record quarantined. That is the intended demonstration of
   "one source failing does not stop the others" — but it makes chaos runs
   mostly-quarantine, so do not benchmark throughput with it.

9. **`"XYZ"` is an accepted currency.** The rule as specified is "3-letter
   alphabetic code, uppercased", and `XYZ` satisfies it. Only `""`, `"US$"`
   and `"999"` are rejected. If real ISO-4217 membership is wanted, that needs
   an explicit allowlist — it is a deliberate gap, not an oversight.

10. **Money never touches binary float.** `_parse_money` routes floats through
    `Decimal(str(value))` (per Phase 3 gotcha #1) and quantizes to 2dp with
    ROUND_HALF_UP, so the `NUMERIC(12,2)` columns never round silently.

11. **`fetch_all_pages` reserves four keyword names** — `source`, `sleep_fn`,
    `stats`, `max_attempts`. Everything else in `**kwargs` is forwarded to the
    fetch function alongside `page`. Watch for collisions if an upstream ever
    grows a parameter with one of those names.

12. **Validation error messages name the *wire* field, not the model field**
    (`campaign_id`, not `campaign_external_id`), because Pydantic reports the
    alias. That is intentional: the reason sits next to the raw payload in
    `quarantined_records`, so they should agree.
