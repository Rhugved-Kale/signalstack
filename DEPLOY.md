# Deploying SignalStack

Three services: **Neon** (Postgres), **Render** (the API), **Vercel** (the
dashboard). All three have a free tier that this app is configured for.

Every step is marked:

- 🖐 **Browser** — you click through a web UI
- ⌨️ **Terminal** — you run a command
- 🤖 **Automatic** — happens without you

There is one unavoidable ordering wrinkle: the API needs to know the
dashboard's URL for CORS, but the dashboard needs the API's URL to build. So
the API is deployed first with a placeholder, and [step 4](#4-close-the-cors-loop)
comes back to fix it. Expect to touch Render twice.

---

## Before you start

⌨️ **Terminal** — push the repo to GitHub (Render and Vercel both deploy from
a Git remote):

```bash
git remote add origin https://github.com/<you>/signalstack.git
```

```bash
git push -u origin master
```

---

## 1. Neon — the database

🖐 **Browser**

1. Go to <https://neon.tech> and sign up (GitHub login is quickest).
2. **Create a project.** Name it `signalstack`. Pick the region closest to
   where you will put Render — `AWS us-west-2 (Oregon)` pairs with Render's
   Oregon region and keeps the round trip short.
3. Neon creates a database called `neondb` and shows a **Connection string**.
   Click **Copy**. It looks like:

   ```
   postgresql://neondb_owner:npg_XXXXXXXX@ep-cool-name-12345678.us-west-2.aws.neon.tech/neondb?sslmode=require
   ```

4. Keep that tab open — you need this string in step 2.

> **Notes**
> - Keep `?sslmode=require`. Neon rejects unencrypted connections, and
>   psycopg honours the flag straight from the URL.
> - You do **not** need to add `+psycopg` to the scheme. `backend/app/config.py`
>   rewrites `postgres://` and `postgresql://` to `postgresql+psycopg://`
>   automatically and leaves the query string alone.
> - Do not create any tables. The API runs `alembic upgrade head` on every
>   boot and then populates an empty database itself.

⌨️ **Terminal** (optional, 30 seconds) — prove the string works before
handing it to Render:

```bash
psql "postgresql://neondb_owner:npg_XXXX@ep-xxx.us-west-2.aws.neon.tech/neondb?sslmode=require" -c "select version();"
```

---

## 2. Render — the API

🖐 **Browser**

1. Go to <https://render.com> and sign up with GitHub. Grant access to the
   `signalstack` repo.
2. **New → Web Service**, then pick the repo.
3. Render detects `render.yaml` at the root and pre-fills most fields. Confirm
   them:

   | Field | Value |
   |---|---|
   | Name | `signalstack-api` |
   | Language / Runtime | `Python 3` |
   | **Root Directory** | **`backend`** ← easy to miss, and nothing works without it |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `./start.sh` |
   | Health Check Path | `/health` |
   | Instance Type | `Free` |

4. Add the **environment variables**:

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the Neon string from step 1, pasted whole |
   | `ENVIRONMENT` | `production` |
   | `CORS_ORIGINS` | `http://localhost:5173` ← placeholder, fixed in step 4 |

5. Click **Create Web Service**.

🤖 **Automatic** — Render clones the repo, installs the pinned requirements,
runs `./start.sh`, which applies the migrations and then starts uvicorn. First
build takes 2-4 minutes.

🤖 **Automatic** — on first boot the API notices the database has no campaigns
and generates a dataset (seed 42, 1200 users) on a background thread. It does
**not** delay the port binding, so the health check passes while data is still
being generated.

### Verify

⌨️ **Terminal** — replace the host with your Render URL:

```bash
curl https://signalstack-api.onrender.com/health
```

Expect one of these, in this order over the first ~30 seconds:

```json
{"status":"ok","service":"signalstack-api","environment":"production","bootstrap":"running","bootstrap_detail":"generating 1200 users"}
```

```json
{"status":"ok","service":"signalstack-api","environment":"production","bootstrap":"complete","bootstrap_detail":"... attribution rows in 6.2s"}
```

Then check there is real data:

```bash
curl "https://signalstack-api.onrender.com/api/summary?model=last_touch"
```

`total_attributed_revenue` should be a non-zero number. 🖐 The interactive docs
at `https://signalstack-api.onrender.com/docs` should also render.

**If the deploy failed**, read the Render log:

| Log line | Cause |
|---|---|
| `Migrations failed — refusing to start the server` | `DATABASE_URL` is wrong or Neon is unreachable. Re-copy the string; keep `?sslmode=require`. |
| `ModuleNotFoundError: No module named 'app'` | **Root Directory** is not set to `backend`. |
| `bash: ./start.sh: Permission denied` | The executable bit was lost. `git update-index --chmod=+x backend/start.sh`, commit, push. |

---

## 3. Vercel — the dashboard

🖐 **Browser**

1. Go to <https://vercel.com> and sign up with GitHub.
2. **Add New → Project**, import the `signalstack` repo.
3. Set:

   | Field | Value |
   |---|---|
   | Framework Preset | `Vite` |
   | **Root Directory** | **`frontend`** |
   | Build Command | `npm run build` (the default) |
   | Output Directory | `dist` (the default) |

4. Expand **Environment Variables** and add:

   | Key | Value |
   |---|---|
   | `VITE_API_URL` | your Render URL, e.g. `https://signalstack-api.onrender.com` |

   No trailing slash. Vite inlines `VITE_*` at **build** time, so changing this
   later requires a redeploy, not just a restart.

5. **Deploy.** Vercel gives you a URL like
   `https://signalstack-<hash>.vercel.app`. **Copy it** — step 4 needs it.

### Verify

🖐 Open the Vercel URL. Because `CORS_ORIGINS` on Render is still the
placeholder, expect the dashboard to sit on **"Waking up the API"** and then
fall through to the error screen after 90 seconds. That is the expected
half-finished state; step 4 fixes it.

---

## 4. Close the CORS loop

This is the step people forget. The API refuses browser requests from origins
it does not know, and the Vercel URL did not exist when you configured Render.

🖐 **Browser**

1. Render dashboard → `signalstack-api` → **Environment**.
2. Edit `CORS_ORIGINS` to your Vercel URL, keeping localhost so local
   development still works — comma-separated, no spaces needed:

   ```
   https://signalstack-<hash>.vercel.app,http://localhost:5173
   ```

3. **Save Changes.** 🤖 Render restarts the service (~30s). The bootstrap
   reports `skipped` this time, because the data is already there.

### Verify

⌨️ **Terminal** — confirm the API now sends the CORS header for your origin:

```bash
curl -si -H "Origin: https://signalstack-<hash>.vercel.app" "https://signalstack-api.onrender.com/api/models" | grep -i access-control-allow-origin
```

You want `access-control-allow-origin: https://signalstack-<hash>.vercel.app`.
No header means the value does not match exactly — check for a trailing slash
or `http` vs `https`.

🖐 Reload the Vercel URL. The full dashboard should render.

> **Custom domains and previews:** every Vercel preview deployment gets its
> own hostname, and none of them are in `CORS_ORIGINS`, so previews will not
> reach the API. Add specific preview URLs as needed, or stick to production.

---

## 5. Living with the free tier

- **The API sleeps after 15 minutes idle** and takes 30-60s to wake. The
  dashboard handles this: it shows "Waking up the API — free hosting sleeps
  when idle" with a progress bar and retries with backoff for 90 seconds
  before showing an error. **Before a live demo, load the page once to wake it.**
- **512 MB RAM.** With `ENVIRONMENT=production` the demo generator is capped
  at 1500 users regardless of what is requested; the job status reports the
  effective count and the dashboard says so. A 3000-user run measured ~120 MB
  peak, so the cap is a margin, not a cliff.
- **Neon suspends compute when idle** too, which adds a second or two to the
  first query. Harmless.
- **Render's free instance restarts on deploy** and re-runs the bootstrap
  check. It is idempotent: with data present it skips in milliseconds.

---

## Redeploying

🤖 Both platforms deploy on push to `master`. Backend changes trigger Render
(migrations included), frontend changes trigger Vercel.

⌨️ To rebuild the demo dataset without redeploying, use the dashboard's **Run
pipeline** button, or:

```bash
curl -X POST https://signalstack-api.onrender.com/api/demo/reset -H 'Content-Type: application/json' -d '{"seed":7,"users":1500,"failure_profile":"normal"}'
```

```bash
curl https://signalstack-api.onrender.com/api/demo/status/<job_id>
```

---

## Configuration reference

| Variable | Where | Required | Notes |
|---|---|---|---|
| `DATABASE_URL` | Render | yes | Neon string; scheme is rewritten automatically, keep `?sslmode=require` |
| `CORS_ORIGINS` | Render | yes | Comma-separated origins; must include the Vercel URL |
| `ENVIRONMENT` | Render | yes | `production` — anything but `local` caps the demo at 1500 users |
| `BOOTSTRAP_ON_EMPTY` | Render | no | `true` by default; `false` leaves a fresh database empty |
| `PORT` | Render | no | 🤖 injected by Render; `start.sh` defaults to 8000 locally |
| `VITE_API_URL` | Vercel | yes | Render URL, no trailing slash, inlined at build time |
