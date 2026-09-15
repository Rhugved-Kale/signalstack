# SignalStack

An ad performance attribution pipeline that ingests campaign, event and revenue
data, scores every conversion under five multi-touch attribution models, and
serves the results to a dashboard.

**Live demo: https://signalstack-ecru.vercel.app**

> The API runs on Render's free tier, which sleeps after 15 minutes idle. The
> first load can take up to 60 seconds while it wakes — the dashboard shows a
> progress indicator and retries automatically, so give it a moment rather than
> reloading.

## The problem

Most marketing reporting credits the last click before a purchase, which
systematically over-credits the bottom of the funnel: search and email get paid
for demand that display and social created. Any other rule for splitting the
credit produces a completely different ranking of which channels are worth
funding. SignalStack computes five of those rules over the same conversions and
puts the disagreement on screen, so the choice of model becomes visible instead
of buried in a reporting default.

## A concrete result

From the live dataset — identical conversions, identical spend, only the
attribution model differs:

| Channel | First-touch | Last-touch | Swing |
| --- | --- | --- | --- |
| display | $24,617.86 | $1,844.46 | **$22,773.40** |
| paid_search | $560.97 | $15,963.88 | $15,402.91 |
| email | $1,618.57 | $11,713.19 | $10,094.62 |

Display is worth **13x more** under first-touch than last-touch. All five models
distribute exactly the same $48,405.73 of revenue; they disagree only about who
earned it, and across all channels $60,998.11 of revenue is contested.

## Architecture

```
  ┌─────────────┐   three fake HTTP APIs that rate-limit, time out,
  │  Generator  │   return 500s, and corrupt ~7% of records
  └──────┬──────┘
         │ paginated JSON
  ┌──────▼──────┐   fetch with retry -> validate -> route -> batch upsert
  │  Ingestion  │   bad records go to quarantine with their raw payload
  └──────┬──────┘
         │
  ┌──────▼──────┐   campaigns, ad_spend, touchpoints, conversions,
  │  Postgres   │   attribution_results, ingestion_runs, quarantined_records
  └──────┬──────┘
         │
  ┌──────▼──────┐   reconstruct each conversion's journey within a 30-day
  │ Attribution │   lookback, score it under all five models
  └──────┬──────┘
         │
  ┌──────▼──────┐   FastAPI: channel performance, model comparison,
  │     API     │   timeseries, journeys, pipeline health
  └──────┬──────┘
         │
  ┌──────▼──────┐   React single page: hero stats, channel bars,
  │  Dashboard  │   model comparison, journey explorer
  └─────────────┘
```

- **Generator** — builds a coherent fake world first (users with real multi-touch
  journeys, not random rows), then serves it through APIs that fail like real
  ones. Deterministic per seed.
- **Ingestion** — walks pagination, retries transport failures, validates every
  record, and quarantines what fails instead of crashing or silently dropping it.
- **Postgres** — natural keys and unique constraints make re-ingestion a no-op.
- **Attribution** — assembles journeys in batches, then scores each one under
  five models in a single pass.
- **API** — thin: validate the query string, call an analytics function,
  serialise.
- **Dashboard** — one page, no router; the model selector re-scores everything.

## Tech stack

Python 3.13, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, psycopg 3, Postgres
16, pytest; React 19, Vite, Recharts; Neon, Render, Vercel.

## Engineering highlights

- **Idempotent ingestion.** Re-running the pipeline changes no row counts. The
  second run of an identical dataset was 8,754 updates and **0 inserts**, with
  zero duplicate natural keys, because every loader upserts on the key the
  schema already enforces.
- **Validation with quarantine, not crash-or-drop.** A malformed record is
  written to `quarantined_records` with its **raw payload preserved** and a
  human-readable reason, so nothing is lost and nothing aborts the run. A single
  bad record costs exactly that record.
- **Dependency-ordered quarantine replay.** One campaign arrived with a dropped
  field, and every child record referencing it was then quarantined as an orphan
  — **462 touchpoints and 58 ad_spend rows from one bad parent**, 56% of all
  quarantine volume. Replay re-fetches parents first, then re-drives children:
  re-fetching that **one** campaign recovered **all 462**.
- **Exact decimal arithmetic.** Credit and revenue are apportioned by the
  largest-remainder method, never multiplied and rounded. Credits sum to exactly
  `1.000000` and attributed revenue reconciles **to the cent** across all five
  models — `$100.01` split three ways is `33.34 + 33.34 + 33.33`, never
  `$100.02`. No float touches the credit path, including the timestamp
  arithmetic behind time decay.
- **Retry with exponential backoff.** 500s and timeouts retry with jittered
  backoff (0.5s → 8s, 5 attempts); a 429 waits exactly its `retry_after` instead
  of guessing. Validation failures are never retried — reproducing a malformed
  record is pointless.
- **Semantic timestamp validation.** A corrupted Unix-epoch timestamp parses
  perfectly and is still absurd. This was caught only when a downstream summary
  endpoint reported a **4-year date range on a 60-day dataset** — a min/max over
  a column turned out to be a better corruption detector than the per-record
  validator, because it asks a question no single record can answer.
- **Self-bootstrapping deployment.** On first boot against an empty database the
  API generates, ingests, replays and scores a dataset on a background thread,
  so a fresh deploy is never a blank dashboard. It never delays the port binding
  (measured: `/health` served in 1.04s while still generating) and reports
  progress through `/health`.
- **~390 tests**, including the penny-leak allocation across 175 model/length/
  amount combinations, idempotency against a real Postgres, and assertions that
  the deployment artefacts stay correct (every requirement exactly pinned,
  `start.sh` executable, no hardcoded API URL in the frontend).

## Local setup

Requires Docker, Python 3.13 and Node 20+.

```bash
git clone https://github.com/Rhugved-Kale/signalstack.git && cd signalstack
```

**1. Start Postgres** (published on host port 5433 to avoid colliding with a
local install):

```bash
docker compose up -d
```

**2. Backend** — create the venv, install, migrate:

```bash
cd backend && python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cd backend && cp .env.example .env && .venv/bin/alembic upgrade head
```

**3. Populate the database** — generate, ingest, replay quarantine, then score:

```bash
cd backend && .venv/bin/python -m app.pipeline.cli --reset --users 3000 --replay
```

```bash
cd backend && .venv/bin/python -m app.attribution.cli --rebuild
```

**4. Run the API** (http://localhost:8000, docs at `/docs`):

```bash
cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000
```

**5. Run the dashboard** (http://localhost:5173), in a second terminal:

```bash
cd frontend && npm install && npm run dev
```

**Tests:**

```bash
cd backend && .venv/bin/python -m pytest -q
```

> The test suite truncates the data tables, so re-run step 3 afterwards if you
> want a populated dashboard.

## How it works

Each conversion's journey is every touchpoint from the same user within a 30-day
lookback, ordered in time. Each model splits that conversion's revenue across
those touchpoints differently:

- **last_touch** — 100% to the final touchpoint. Assumes whatever closed the sale
  caused it. Flatters search and email.
- **first_touch** — 100% to the first touchpoint. Assumes discovery is everything
  and nurture is free. Flatters display and social.
- **linear** — equal credit to every touchpoint. Assumes no touch matters more
  than another; wrong, but unbiased about which direction it is wrong in.
- **time_decay** — credit decays exponentially toward the conversion with a
  7-day half-life. Assumes influence fades.
- **position_based** — 40% first, 40% last, 20% shared by the middle. Assumes
  discovery and closing are the hard parts.

None of them is correct. They encode different beliefs about how marketing
works, and the dashboard exists to make the size of that disagreement legible.

## Deployment

See [DEPLOY.md](DEPLOY.md) for Neon, Render and Vercel setup, and
[DEMO.md](DEMO.md) for a walkthrough of the live app. [PROGRESS.md](PROGRESS.md)
is the build log, one section per phase, including the gotchas each phase hit.
