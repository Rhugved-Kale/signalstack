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

## Phase 4.5 — Quarantine replay

Date: 2026-09-14

Two targeted changes: a semantic currency allowlist, and dependency-ordered
replay of quarantined records. No attribution logic, endpoints or frontend.

### Change 1 — ISO-4217 currency allowlist

`schemas.py` now checks membership in `SUPPORTED_CURRENCIES` (USD, EUR, GBP,
CAD, AUD, JPY, CHF, SEK, NOK, DKK, NZD, MXN) instead of "three alphabetic
characters". Values are uppercased first, so `"usd"` is still accepted and
stored as `"USD"`. Rejections name the value:

    currency: 'XYZ' is not a supported currency code

**Why shape-validity and semantic-validity are different things.** The old rule
asked "does this *look* like a currency code?" — and `"XYZ"` passes that test
perfectly: three letters, all alphabetic. It is also not money this pipeline
can do anything with. A shape check can only reject malformed input; it cannot
reject well-formed input that the system has no meaning for. Accepting `"XYZ"`
would have written a row whose `revenue` is denominated in nothing, and every
downstream sum over mixed currencies would be silently wrong — the worst class
of bug, because it produces plausible numbers. Supporting a new currency is a
deliberate act (FX rates, reporting, reconciliation), so the list of currencies
we can actually handle belongs in code as an explicit constant, not implied by
a regex. Phase 4 gotcha #9 flagged this as a deliberate gap; it is now closed.

Note: in the seed-42 / 3,000-user run no `"XYZ"` corruption happened to land on
a conversion record, so the allowlist did not change that run's counts. Its
effect is covered by a unit test asserting the exact rejection string, plus
tests that every allowlisted code is accepted in either case.

### Change 2 — `pipeline/replay.py`

    replay_quarantine(db, *, sources=None, max_rounds=3, fetch_parents=True,
                      world_seed=42, world_config=None, failure_config=None,
                      sleep_fn=None) -> dict

Processes quarantine in dependency order — `campaigns`, `ad_spend`,
`touchpoints`, `conversions` — reusing the existing schemas, loaders and
fetcher rather than reimplementing any of them.

The insight it exploits: a quarantined *campaign*'s stored payload is corrupt,
so re-validating it will fail forever. But the entity it describes is still
fetchable, and the upstream corrupts randomly per call. So:

1. **Re-fetch the parents.** `_refetch_missing_campaigns` pulls the campaigns
   source through the normal retry path, validates, and upserts only campaigns
   that were *missing* from the database — so the returned count means exactly
   "parents this repaired" and existing rows are untouched.
2. **Re-drive the children.** Their payloads were never corrupt; they were
   merely orphaned. With the parent present they load cleanly.
3. **Recovered rows are deleted** from `quarantined_records`; rows that fail
   again **stay**, with `error_reason` refreshed to the current failure and
   `raw_payload` left untouched.
4. Loops up to `max_rounds`, stopping early as soon as a round recovers
   nothing.
5. Records the whole operation as an `IngestionRun` with source `"replay"`,
   preserving the `received == ingested + quarantined` identity.

### Results (seed 42, 3,000 users)

| source | quarantined | recovered | remaining |
| --- | --- | --- | --- |
| campaigns | 1 | 1 | 0 |
| ad_spend | 87 | 58 | 29 |
| touchpoints | 804 | **462** | 342 |
| conversions | 32 | 0 | 32 |
| **TOTAL** | **924** | **521 (56.4%)** | **403** |

Re-fetching **one** campaign (`cmp_05da846a`, quarantined because
`campaign_name` was dropped) recovered **all 462 orphaned touchpoints and all
58 orphaned ad_spend rows** — exactly the cascade Phase 4 gotcha #1 predicted,
now repaired. Row counts moved campaigns 11 → 12, touchpoints 7,417 → 7,879,
ad_spend 633 → 691, and zero `unknown campaign_id` rows remain. The surviving
403 are genuinely malformed (dropped required fields, garbage timestamps, empty
ids) and are not recoverable by replay.

### CLI

```bash
cd backend && .venv/bin/python -m app.pipeline.cli --reset --users 3000 --replay
```

```bash
cd backend && .venv/bin/python -m app.pipeline.cli --replay-only
```

`--replay` runs replay after ingestion; `--replay-only` replays existing
quarantine with no fresh ingestion. Both print a before/recovered/remaining
table. `--reset --replay-only` is refused by the parser, since it would
truncate the quarantine you are asking it to replay.

### Notes / gotchas for future sessions

1. **Replay MUST fetch with a different RNG seed than the ingestion did.**
   The fake APIs are deterministic per seed, so re-fetching with the
   ingestion's own seed reproduces byte-identical corruption and recovers
   *nothing*. `replay.py` offsets the seed by
   `REPLAY_SEED_BASE + round * REPLAY_SEED_STRIDE` (1000 + round*17), which is
   different from ingestion but still deterministic — so tests stay
   reproducible. This is the single most important detail in the module; if
   replay ever "mysteriously recovers 0 parents", check this first.

2. **A quarantined campaign is recovered by identity, not by re-validation.**
   Its stored payload stays invalid forever. Replay instead checks whether the
   payload's `campaign_id` now exists in `campaigns` (having been repaired by
   the re-fetch) and, if so, deletes the obsolete quarantine row. A campaign
   whose *`campaign_id` itself* was the corrupted field cannot be identified
   this way and will correctly remain quarantined — there is genuinely no way
   to know which campaign it was.

3. **`--users` does not have to match the original ingestion for
   `--replay-only`.** `build_world` generates campaigns *before* users, so the
   campaign set depends only on `seed` and `n_campaigns`. A `--users` mismatch
   therefore cannot inject spurious campaigns during a parent re-fetch. Do not
   rely on this if generation order ever changes.

4. **`fetch_parents=False` makes replay a no-op for orphans**, by design — it
   is the switch for "re-validate stored payloads only, do not touch the
   network". A test asserts it recovers nothing and inserts no campaigns.

5. **Replay is idempotent, but the audit tables still grow.** A second replay
   recovers 0, stops after one round, leaves every data-table count unchanged,
   and does not resurrect deleted quarantine rows — but it does append another
   `IngestionRun`. Same append-only rule as Phase 4 gotcha #5.

6. **Requesting only child sources still repairs parents.** `sources=
   ["touchpoints"]` triggers the campaign re-fetch, because the whole point is
   to un-orphan the children. The re-fetch is skipped only when no selected
   source references a campaign (i.e. `conversions` alone) or when
   `fetch_parents=False`.

7. **Replay reuses the loaders, so in-batch dedupe applies here too.** A model
   the loader did not reject is treated as recovered, including one dropped as
   an in-batch duplicate — its twin carried the data into the table, so the
   quarantine row is genuinely obsolete.

## Phase 5 — Attribution engine

Date: 2026-09-14

Scope: the attribution engine and its rollups. No API endpoints, no frontend —
Phase 6 wraps `analytics.py`.

### Files added

- `backend/app/attribution/journeys.py` — batched journey assembly.
- `backend/app/attribution/models.py` — the five models + exact apportionment.
- `backend/app/attribution/engine.py` — scoring runner with idempotent upserts.
- `backend/app/attribution/analytics.py` — five read-only rollups.
- `backend/app/attribution/cli.py` — `python -m app.attribution.cli`.
- `backend/tests/test_attribution.py` — 251 tests.

### The five models, and what each believes about marketing

| Model | Rule | Implicit assumption | Who it flatters |
| --- | --- | --- | --- |
| `last_touch` | 100% to the final touch | whatever closed the sale caused it | late funnel (paid search, email) |
| `first_touch` | 100% to the first touch | discovery is everything, nurture is free | early funnel (display, social) |
| `linear` | equal split | no touch matters more than another | long journeys, mid funnel |
| `time_decay` | `0.5 ** (hours_before / half_life)`, half-life 7 days | influence fades with time | recent touches, whatever they are |
| `position_based` | 40% first, 40% last, 20% shared by the middle | discovery and closing are the hard parts | both ends, penalises the middle |

Special cases for `position_based`: one touchpoint takes 100%, two split
50/50 (there is no middle to pay).

**The whole point is that they disagree.** On the seed-42 dataset, display is
worth \$2,220 under `last_touch` and \$57,931 under `first_touch` — a 26x
spread on identical data, because Phase 3 deliberately biased display toward
the *start* of journeys. `paid_search` swings the opposite way (\$42,608 vs
\$1,067). Total disputed revenue across channels is \$163,305 against
\$124,804 attributed, so on average every dollar's owner is contested.

### Exactness: largest-remainder apportionment

Credits and revenue are apportioned, not multiplied-and-rounded:

1. Convert the model's raw weights into integer units — 10^6 units for credit
   (6 decimal places, matching `NUMERIC(8,6)`), or the conversion's exact cent
   total for revenue.
2. Floor every share.
3. Hand the leftover units out one at a time to the largest truncated
   remainders, **ties broken by touchpoint id**.

Consequences, all test-enforced:

- Credits sum to exactly `Decimal("1.000000")`. Not 0.999999.
- Attributed revenue sums to exactly the conversion's revenue to the cent.
  `Decimal("100.01")` split three ways gives `33.34 + 33.34 + 33.33`, never
  `100.02` or `99.99`.
- Results are deterministic: the tie-break on touchpoint id means the same
  journey always produces the same vector.

There is **no float anywhere in the credit path**, including the timestamp
arithmetic — `timedelta.total_seconds()` returns a float, so `TimeDecay`
combines `days`/`seconds`/`microseconds` into a `Decimal` by hand instead.

### Lookback window

A touchpoint joins a conversion's journey when it shares the `user_id`,
occurred at or before the conversion, and is within `lookback_days` (default
30) of it. Ordering is `occurred_at` ascending, ties broken by touchpoint id
so the sequence is total.

On the current dataset 8 of 693 conversions have **no** touchpoint in the
window. That is a real outcome (untracked/direct), not an error: the journey
is yielded empty, every model returns `[]`, and the conversion is counted in
`conversions_with_empty_journey`.

### Performance

`load_journeys` walks conversions in keyset-paginated batches and fetches all
touchpoints for a batch's users in **one** query, so the cost is 2 queries per
batch rather than 1 per conversion: **693 journeys in 4 queries**. The query
count is logged on every pass.

### Verification

`--rebuild` then a second run with no flags: `attribution_results` stayed at
**8,273 rows**, with `rows_inserted` going 8,273 → **0** and `rows_updated`
0 → **8,273**, and zero duplicate `(conversion_id, touchpoint_id, model_name)`
keys.

Revenue reconciles to the cent: total conversion revenue \$126,197.69 =
\$124,804.39 attributed (identical under all five models) + \$1,393.30 from
the 8 empty-journey conversions.

### Notes / gotchas for future sessions

1. **Only non-zero credits are written.** A zero-credit row would assert "this
   touchpoint got nothing", which its absence already says, and it would make
   `touchpoint_count` in `channel_performance` mean something different for
   `last_touch` (all touchpoints in every journey) than for `linear` (the
   credited ones). A row in `attribution_results` means credit was assigned.
   The models themselves still return a full vector including zeros — that is
   the mathematical object, and the tests check it.

2. **Every model attributes an identical total.** That is the strongest
   invariant in the phase: if two models disagree on the *total*, there is a
   bug in the apportionment, not a difference of opinion. Only the
   distribution across channels should differ.

3. **`rebuild=True` is required when a model's definition or the lookback
   changes.** An upsert can add and update rows but cannot delete rows that
   should no longer exist — e.g. shrinking the lookback orphans credits for
   touchpoints that have dropped out of the window. `--rebuild` deletes only
   the selected models' rows, so a single-model rebuild leaves the others
   intact (test-enforced).

4. **A touchpoint can belong to several journeys.** A repeat purchaser's early
   touches sit inside the lookback window of more than one conversion, so
   `rows_written` (rows upserted) and `touchpoints_credited` (distinct
   touchpoint ids) are genuinely different numbers. They happen to be equal on
   the current dataset because repeat purchases are rare here.

5. **ROAS is `None`, never 0 or an error, when spend is zero.** Organic has no
   spend by definition. `channel_performance` also includes channels that cost
   money but earned no credit — those are exactly the ones worth questioning.

6. **`email` shows a ~197x ROAS. That is an artifact, not a finding.** Phase 3
   models email cost as near-zero (per-send, effectively rounding error), so
   its denominator is tiny. Do not present it as a real result in the
   dashboard without a caveat.

7. **Spend is joined to channels separately from attribution.** Joining
   `ad_spend` into the attribution aggregate would multiply spend by the number
   of attribution rows per campaign. `_spend_by_channel` is a second query,
   merged in Python, for exactly that reason.

8. **`date_trunc` granularity is whitelisted.** It takes a SQL literal, so
   `timeseries` validates against `GRANULARITIES` rather than interpolating
   the caller's string.

9. **Date filters use explicit UTC datetime bounds**, not `CAST(... AS date)`,
   so results do not depend on the database session's timezone.

10. **`pytest` truncates the data tables** (the `db` fixtures in
    `test_pipeline.py` and `test_attribution.py`). Re-run the Phase 4 pipeline
    CLI before any attribution demo, or the engine will correctly report zero
    journeys. Order for a full demo: pytest → `app.pipeline.cli --reset
    --replay` → `app.attribution.cli --rebuild`.

11. **`credit` is `NUMERIC(8,6)`**, which holds `[0, 99.999999]` — fine for a
    fraction in `[0, 1]` at exactly 6 decimal places. If a future model ever
    emits credits above 1 (e.g. an uplift model), the column needs widening.

## Phase 6 — API layer

Date: 2026-09-14

Scope: HTTP endpoints only. No frontend. The layer is deliberately thin —
every route validates its input, calls an existing `analytics.py` or runner
function, and serialises the result. There is no attribution or pipeline logic
in `app/api/`.

### Files added

- `backend/app/api/schemas.py` — Pydantic v2 response models.
- `backend/app/api/routes.py` — the endpoints, mounted under `/api`.
- `backend/app/api/jobs.py` — in-process job registry for the demo button.
- `backend/app/api/__init__.py` — exports the router.
- `backend/app/main.py` — rewritten: router, middleware, exception handlers,
  OpenAPI metadata. `GET /health` is byte-for-byte unchanged.
- `backend/tests/test_api.py` — 34 tests.

### Endpoints

| Method | Path | Backed by |
| --- | --- | --- |
| GET | `/health` | unchanged from Phase 1 (deployment probes use it) |
| GET | `/api/models` | static catalogue with a description per model |
| GET | `/api/channels` | `channel_performance()` |
| GET | `/api/model-comparison` | `model_comparison()` |
| GET | `/api/timeseries` | `timeseries()` |
| GET | `/api/journeys` | `top_journeys()` |
| GET | `/api/pipeline/health` | `pipeline_health()` |
| GET | `/api/summary` | composed from the three above |
| POST | `/api/demo/reset` | `reset_data` → `run_ingestion` → `replay_quarantine` → `run_attribution` |
| GET | `/api/demo/status/{job_id}` | the job registry |

Validation: `model` and `granularity` are `Enum`s, so FastAPI documents the
valid values and rejects anything else with a 422 that lists them;
`start_date > end_date` raises a 422 naming both dates; `limit` is a hard cap
at 100 (`le=100` → 422) rather than a silent clamp, because silently returning
20 rows when 500 were asked for is worse than saying no.

### The demo reset job pattern

`POST /api/demo/reset` returns **202 immediately** with a job id and kicks the
work into a Starlette `BackgroundTask`. Job state lives in one module-level
dict guarded by a `threading.Lock`: status (`running`/`success`/`failed`),
`started_at`, `finished_at`, per-stage progress with details, and an error
message. A second request while one is in flight gets **409** with the active
job id. History is pruned to the last 20 jobs so the dict cannot grow forever.

The background worker opens its **own** `SessionLocal` — the request's session
is already closed by the time it runs — and records per-stage detail as it
goes, so the frontend can show "ingest: 9,862 received, 924 quarantined" while
the run is still going.

Verified end to end: the endpoint rebuilt the full 3,000-user dataset in 7.5s
and reproduced the CLI's numbers exactly (9,862 received → 521 replayed →
8,273 attribution rows).

### Error handling

- Unhandled exceptions → 500 `{"error", "detail"}`, traceback to the log only.
- `OperationalError` / `InterfaceError` → **503** with an actionable message
  ("check that Postgres is running"), because a dead database is not the
  client's fault and the request is worth retrying.
- Request-logging middleware logs `method path -> status in Xms` and adds an
  `X-Response-Time-ms` header.

### Notes / gotchas for future sessions

1. **`Decimal` becomes a JSON number at this boundary, and only here.**
   Pydantic v2 serialises `Decimal` as a *string* by default, which would make
   every chart library in Phase 7 parse strings. `JsonDecimal` (a
   `PlainSerializer` applied `when_used="json"`) emits a number while the
   Python-side value stays `Decimal`. It is defined once in `api/schemas.py`;
   do not hand-roll per-field serialisers. The float conversion happens at the
   wire, never in the attribution arithmetic.

2. **`roas` is `float | None` and null is meaningful.** Organic has no spend,
   so it has no ROAS. The frontend must render "n/a", not "0.00x".

3. **TestClient drains background tasks before returning the response.** A
   real first `POST /api/demo/reset` has already *finished* by the time a
   second request could arrive under test, so the 409 guard cannot be tested
   with two sequential client calls. `test_second_demo_reset_while_one_is_
   running_returns_409` registers a running job directly in the registry
   instead. If that test ever looks redundant, this is why it is written that
   way.

4. **Testing the generic 500 handler needs `raise_server_exceptions=False`.**
   Starlette's `ServerErrorMiddleware` re-raises unhandled exceptions into the
   test client by default, so the assertion would never see the response body.
   Handlers registered for a *specific* exception class (like
   `OperationalError` → 503) are handled by `ExceptionMiddleware` and work
   with a plain `TestClient`.

5. **`/api/summary` is the one composed endpoint.** It sums
   `channel_performance`, takes the top row of `model_comparison` (already
   sorted by swing) and reads counts from `pipeline_health`. The only SQL in
   the whole API layer is `_conversion_date_range`'s `min`/`max` — a metadata
   lookup describing the dataset's extent, not a performance rollup, which is
   why it did not go in `analytics.py`.

6. **The API surfaced a real data bug that is not an API bug.**
   `/api/summary` reports `date_range.start = "2022-12-11"` on a dataset that
   covers 60 days. Cause: Phase 3's `timestamp_unix_int` corruptor emits
   `randint(1_600_000_000, 1_800_000_000)` (2020-09-13 … 2027-01-15) and
   Phase 4's validation accepts Unix epoch integers *by specification*, so
   those corrupted values parse cleanly and are ingested. 2 conversions and
   108 touchpoints currently sit outside the world window. Fixing it means
   adding a plausibility window to `_parse_timestamp`, which is ingestion
   work, not API work — deliberately left alone here. The out-of-window
   touchpoints also fall outside the 30-day lookback, so they are silently
   excluded from journeys.

7. **`app.pipeline.cli` is imported for `FAILURE_PROFILES` and `reset_data`.**
   That couples the API to a CLI module. It is fine for now, but if either
   grows, move both into a shared `app/pipeline/profiles.py`.

8. **`status.HTTP_422_UNPROCESSABLE_ENTITY` is deprecated** in this Starlette
   version; use `HTTP_422_UNPROCESSABLE_CONTENT`. The remaining two warnings
   in the test run come from Starlette/httpx internals, not this codebase.

9. **Job state is lost on restart.** Acceptable for a demo control. If it ever
   needs to survive a deploy, it belongs in a table, not a dict.

## Phase 6.5 — Timestamp plausibility

Date: 2026-09-14

Scope: one semantic check in `backend/app/pipeline/schemas.py`, plus tests.
No new endpoints, no frontend.

### Shape-validity is not semantic-validity

Phase 4's timestamp parser asked one question: *can this be parsed?* Phase 3's
`timestamp_unix_int` corruptor replaces a timestamp with
`randint(1_600_000_000, 1_800_000_000)` — a random epoch integer spanning
2020-09-13 to 2027-01-15. Every one of those parses perfectly. They are real,
well-formed instants. They are simply absurd for a dataset covering the last
60 days.

That is the distinction this phase is about, and it is the same one Phase 4.5
drew for currencies: `"XYZ"` is a well-formed three-letter code and still not
money we can attribute. A parser can only reject malformed input; it cannot
reject well-formed input the system has no meaning for. That needs a second,
semantic check — and the two must stay separate, because a value can fail one
without the other.

`_parse_timestamp` / `_parse_date` remain pure shape parsers. New wrappers
`_validate_timestamp` / `_validate_date` run the shape parse first, then apply
the plausibility window, so an unparseable value still reports
`unparseable timestamp` rather than a confusing plausibility error
(test-enforced).

### The window

```python
MAX_TIMESTAMP_AGE  = dt.timedelta(days=730)  # ~2 years before ingestion
MAX_TIMESTAMP_SKEW = dt.timedelta(days=1)    # tolerated clock skew ahead
```

Deliberately generous. The job is to catch corruption, not to enforce the
dataset's exact window: backfilling genuinely old data is legitimate, and a
source system whose clock runs an hour ahead of ours is normal — treating that
as corruption would quarantine good data. Rejections name the value and the
reason:

    created_at: 1676632583 is outside the plausible window (more than 2 years in the past)
    timestamp: '2026-09-17T12:00:00Z' is outside the plausible window (more than 1 day in the future)

Applies to `TouchpointIn.occurred_at`, `ConversionIn.occurred_at` and
`AdSpendIn.date`.

The reference "now" is injectable through Pydantic's validation context —
`model_validate(raw, context={"now": ...})` — so tests are deterministic
instead of depending on the wall clock. Production passes no context and gets
the real clock.

### It was caught by a downstream view, not by the validator

Worth recording how this surfaced. The validator was happy. The pipeline
reported no errors. Every test passed. What exposed it was building
`GET /api/summary` in Phase 6 and noticing that `date_range` read
**2022-12-11 → 2026-09-14** for a dataset that covers 60 days.

Nothing upstream was in a position to notice: each layer did exactly its job
on each record in isolation, and the anomaly only existed in the *aggregate*.
A min/max over a column turned out to be a better corruption detector than the
per-record validator, because it asks a question no single record can answer.
The lesson is to build the aggregate view early — it audits the layers beneath
it for free.

### Results

Re-running `--reset --users 3000 --replay` then `--rebuild`:

| | before | after |
| --- | --- | --- |
| `/api/summary` `date_range` | 2022-12-11 → 2026-09-14 | **2026-07-20 → 2026-09-14** |
| conversions outside the 60-day window | 2 | **0** |
| touchpoints outside the 60-day window | 108 | 27 |
| conversions / touchpoints | 693 / 7,879 | 691 / 7,798 |
| quarantined records | 403 | 496 |

93 records were newly quarantined by the check: 84 touchpoints, 7 ad_spend, 2
conversions. Every model still attributes an identical total
(\$124,804.39, credit exactly 685.000000 each), and revenue still reconciles
to the cent: \$125,958.67 total = \$124,804.39 attributed + \$1,154.28 across
6 empty-journey conversions.

### Notes / gotchas for future sessions

1. **27 touchpoints are still outside the 60-day world window (2024-09-29 to
   2026-06-16), and that is expected.** A 2-year plausibility window cannot
   catch a corrupted epoch that happens to land inside 2 years — roughly a
   third of the corruptor's `1.6e9 … 1.8e9` range falls within it. The
   conversions were all caught because both bad ones happened to land in
   2022-23. If the remaining 27 ever matter, the fix is a *different* check —
   validating against the ingestion run's own world window rather than a
   generic horizon — which is a narrower, dataset-aware rule and a deliberate
   non-goal here. `ad_spend.date` likewise still has one row at 2025-08-07.

2. **A Pydantic `BeforeValidator` only receives `ValidationInfo` if the
   function takes exactly two parameters with no default.** Writing
   `def f(value, info=None)` silently makes it a one-argument validator and
   `info.context` is never delivered — the injected `now` would be ignored and
   the tests would pass against the wall clock by accident. Verified
   empirically before relying on it.

3. **The two rejected conversions were exactly the ones with empty journeys**,
   which is why total attributed revenue did not change at all. Their
   timestamps were years away from any touchpoint, so nothing fell inside
   their 30-day lookback and they contributed nothing to attribution.
   Consistent, and a useful sanity signal.

4. **The runner does not pin a single reference "now" per run.** Each record is
   judged against the clock at the moment it is validated, so a very long
   ingestion could in principle apply a drifting boundary. Irrelevant at
   current runtimes (seconds), but if runs ever take hours, pass
   `context={"now": run_started_at}` from `runner.py` for a stable boundary.

## Phase 7 — React dashboard

Date: 2026-09-14

Scope: `frontend/` only. No backend file was touched. One page, no router, no
state library, no UI kit — recharts is the single dependency added.

### Component structure

```
frontend/src/
  api.js              # every fetch; the only place that knows the API URL
  selectors.js        # pure derivations over API responses
  theme.js            # the two colour scales, defined once
  format.js           # money / count / roas / date formatters
  hooks/
    useApi.js         # loading / error / refresh-without-flash
    usePrefersDark.js # OS colour scheme, for the dark chart steps
  App.jsx             # layout, model state, refreshKey, page-level error gate
  components/
    primitives.jsx        # Card, Skeleton, InlineError, EmptyState, Badge
    ModelSelector.jsx     # the one filter row, above everything it scopes
    HeroStats.jsx         # §1 stat cards + the disagreement spotlight
    ChannelPerformance.jsx# §2 horizontal bars + table
    ModelComparison.jsx   # §3 grouped bars + swing table
    JourneyExplorer.jsx    # §4 left: expandable customer paths
    PipelineHealth.jsx    # §4 right: runs, quarantine, row counts
    RunPipeline.jsx       # confirm → POST → poll → refresh
```

### The API client

`api.js` reads `VITE_API_URL` once and exports one function per endpoint.
Everything goes through a single `request()` that normalises failures into an
`ApiError` with `.status` and `.isNetworkError`, so callers can tell "the
backend is down" (status 0) from "that model does not exist" (422) without
string matching. No component contains a URL.

`useApi(fetcher, deps)` owns the three states. On a refetch it **keeps the
previous data** and sets `isRefreshing`, so sections dim instead of collapsing
into skeletons — no layout jump when the model changes. Each call gets an
`AbortController`, and aborted requests are swallowed rather than rendered as
errors.

One `refreshKey` in `App` is the refetch-everything switch: the pipeline button
bumps it and all six calls re-run.

### Colour

Two categorical scales in `theme.js`, both taken in fixed slot order from a
palette validated with the data-viz skill's checker (worst adjacent CVD ΔE 9.1
light / 8.4 dark; normal-vision ΔE 19.6 / 19.3; all slots inside the lightness
and chroma bands):

* **Channel colours** — identity. A channel keeps its hue in every chart,
  table swatch and journey chain. Bars re-order when the model changes; the
  colours do not follow rank.
* **Model colours** — used only in §3, where the *series* is the model and the
  channel is carried by the axis label instead. Colouring those five bars by
  channel would make the models indistinguishable.

Three light-mode hues fall under 3:1 contrast against the surface, so the
validator's relief rule applies: every chart ships a table view and direct
value labels. Dark mode uses separately-stepped hues, not dimmed light ones.

### Gotchas

1. **`/api/journeys` returns only *credited* touchpoints, so single-touch
   models lose the path.** Under `last_touch` every journey came back with one
   touchpoint and the chain collapsed to a single node — the panel's whole
   point is the path. Fixed in the frontend (the backend was off-limits, and
   the response is not wrong): for single-touch models the path is sourced
   from `linear`, which credits every touchpoint and therefore describes the
   full journey, and the selected model's credits are overlaid on it by
   `(conversion_id, touchpoint_id)`. Uncredited touchpoints render as 0%,
   which turns out to be the clearest demonstration on the page — expanding a
   `last_touch` journey shows 0%, 0%, 0%, 100%. Both fetches use `limit=100`
   so the overlay finds its matches; see `buildJourneyList`.

2. **An inline `<span>` has no height.** `.credit-bar-fill` was
   `display: inline` with `height: 100%`, so every credit bar rendered at 0px
   — invisible, with no error anywhere. Caught by inspecting computed styles
   in the browser, not by the build. Both the bar and its track are now
   explicitly `display: block`.

3. **`ERR_ABORTED` in the network log is expected in dev.** React 19's
   StrictMode mounts effects twice, so `useApi`'s cleanup aborts the first
   request of each pair; every abort is immediately followed by a 200. It does
   not happen in a production build.

4. **The pipeline button is deterministic, so "the numbers update" is subtle.**
   Seed 42 rebuilds byte-identical data, so after a run the figures are the
   same — the refetch genuinely fires (visible in the network log) but nothing
   appears to change. That is the generator working as designed; change the
   seed in `RunPipeline.jsx`'s `DEMO_PAYLOAD` to see the numbers move.

5. **The 409 path needs an external trigger to see.** The button disables
   itself while a run is in flight, so the "already running" case only arises
   from another tab or client. Verified by starting a run with `curl` and then
   clicking the button; it shows an informational banner, not an error.

6. **`recharts` is ~610 kB raw / 181 kB gzipped**, which trips Vite's 500 kB
   chunk advisory. Left as one chunk on purpose: the charts are needed for
   first paint, so splitting them would move bytes without improving the load.

7. **Bundle-level date handling.** All dates are formatted through
   `format.js`; nothing calls `toLocaleString` inline, so the `—` fallback for
   null/NaN is applied in exactly one place per type.
