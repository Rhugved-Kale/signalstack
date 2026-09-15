# Demo notes — SignalStack

Speaking notes, not a script. Numbers below are from the live deployment; they
change if someone hits **Run pipeline**, so glance at the page before you talk.

- Dashboard: https://signalstack-ecru.vercel.app
- API docs: https://signalstack-9mqj.onrender.com/docs

---

## 30 seconds before you start

1. **Open the live URL now.** The API sleeps after 15 minutes idle and takes
   30-60s to wake. The dashboard handles it gracefully — progress bar, auto
   retry — but you do not want to narrate that for a minute.
2. Wait for the numbers to render, then leave the tab open.
3. Have `/docs` open in a second tab if you plan to go deep.
4. Optional: click **Run pipeline** once so the data is fresh and you know what
   the current headline number is.

If it is still waking when you start, say so plainly: "free tier, it sleeps,
this is the cold start — it handles it, here's the retry UI." That reads as
having thought about it, not as a failure.

---

## The 60-second version

> "SignalStack is an ad attribution pipeline. It takes campaign spend, user
> events and orders, reconstructs each customer's journey, and works out which
> marketing channel actually earned the revenue.
>
> The interesting part is that there's no single right answer. Point at the
> callout: **display is credited about $24,600 under first-touch and about
> $1,800 under last-touch** — the same conversions, the same spend, a 13x
> difference in what that channel looks like it's worth. Most reporting tools
> pick one rule and hide it. This one computes five and shows you the
> disagreement, because that's the number a marketing team is actually
> arguing about."

Then stop. Let them ask.

---

## The 3-minute version

Walk the four sections top to bottom.

### 1. Hero stats + the disagreement callout

**Say:** every model distributes exactly the same total revenue — $48,405.73 —
so this isn't a rounding argument, it's a question of attribution. The callout
names the most contested channel automatically.

**Click:** the model selector. Switch last-touch → first-touch.

**Point out:** the totals don't move, but everything about the split does.

### 2. Channel performance

**Click:** switch the model again and let them watch the bars re-order and
animate.

**Say:** each channel keeps its colour as the ranking changes — colour follows
the channel, not its rank, so you can track display moving from bottom to top.

**Point out:** organic shows ROAS as an em dash, not `0.00x` or `Infinity`.
Zero spend means ROAS is undefined, not zero, and the whole stack carries that
distinction through to the UI.

### 3. Model comparison — the centrepiece

**Say:** five bars per channel, one per model. A tall spread inside a group
means that channel's apparent value depends almost entirely on which model you
believe.

**Point out:** display and paid_search are near mirror images. That's not
noise — the data generator deliberately biases display toward the *start* of
journeys and paid search toward the *end*, so the models diverge in a
direction I can predict and check.

**Point out:** the swing column, sorted descending, and the total: about
**$61,000 of contested revenue against $48,000 attributed**. On average every
dollar's owner is disputed by more than one model.

### 4. Journey explorer + pipeline health

**Click:** expand a journey row with 4+ touchpoints.

**Say:** this is one customer's actual path. Under last-touch you see three
touchpoints at **0.0%** and the final one at **100%** — that's what last-click
attribution does, made concrete on a single order.

**Click:** switch to linear and re-expand. Now the credit is split evenly and
the dollars still sum to the order total exactly.

**Then the right-hand panel.** Say: this surfaces the pipeline's own data
quality rather than pretending the inputs were clean. **203 records are
quarantined** with their reasons — empty user ids, unparseable timestamps like
`'0000-00-00'` and `'yesterday'`. That's the validation layer working, not an
error state.

---

## The deep-dive version

Reach for these when they push on engineering rather than product.

### /docs — the API surface

Open https://signalstack-9mqj.onrender.com/docs. Nine endpoints. Point out
`/api/demo/reset` returns **202 with a job id** and runs the pipeline in the
background with per-stage progress, because a 30-second synchronous request is
not an API.

### The quarantine table — the design decision worth defending

**Say:** when a record is malformed there are three options: crash the run, drop
the record, or keep it. Crashing means one bad row costs you the whole batch;
dropping means silent data loss you find out about in a board meeting. So bad
records go to `quarantined_records` with the **raw payload preserved verbatim**
and a human-readable reason, and the run continues. Every counter reconciles:
`records_received == records_ingested + records_quarantined`, exactly, per
source.

### The replay cascade — the best story in the project

**Say:** one campaign record arrived with its name field dropped, so it failed
validation and got quarantined. Its `campaign_id` was intact — but the campaign
row never landed. Every touchpoint and spend row referencing it then failed on
referential grounds: **462 touchpoints and 58 spend rows orphaned by one bad
parent**, which was 56% of all quarantine volume.

**Then:** the fix is ordering. Replay processes quarantine in dependency
order — parents before children. A quarantined campaign's stored payload is
corrupt forever, so re-validating it is pointless; instead it **re-fetches** the
campaign from the upstream, which corrupts randomly per call, so a retry
usually returns it intact. Re-fetch that one parent, and all 462 children
replay successfully.

**The lesson to name:** the highest-value recovery action was not "retry
everything", it was "retry the *right thing first*."

### The bug that only a downstream view could find

**Say:** the generator corrupts some timestamps into random Unix epoch
integers. Those parse perfectly — they're real instants — so the validator
accepted them. Nothing failed. Every test passed.

What caught it was building the summary endpoint and noticing `date_range`
read **2022 to 2026 for a dataset covering 60 days**. A min/max over a column
was a better corruption detector than the per-record validator, because it asks
a question no single record can answer. Fix was a plausibility window: reject
timestamps more than two years old or more than a day in the future, separate
from the shape check.

---

## Likely questions, honest answers

**"Why five models?"**
Because the choice of model is an assumption, not a calculation, and single-model
reporting hides it. Storing all five side by side turns "which channel is worth
funding" into a visible range instead of one number that depends on a default
someone picked. The swing column is the actual deliverable.

**"Which one is correct?"**
None of them. They're not competing estimates of a hidden truth — they encode
different beliefs about how marketing works. Last-touch assumes the closer
caused the sale; first-touch assumes discovery did. Both are obviously
incomplete. The honest framing is that attribution is a budgeting convention,
and the useful output is the spread, not a winner. If someone forced me to pick
one as a default I'd take position-based or time-decay, because they at least
credit more than one touch — but I'd show the range alongside it.

**"Why is email ROAS 75x?"**
Because my cost model is wrong, not because email is magic. I modelled email
cost as near-zero — per-send, effectively rounding error — so the denominator is
tiny and the ratio explodes. It's an artifact of the synthetic generator. I'd
flag it rather than ship it: either give email a realistic platform cost, or
suppress ROAS for channels below a spend floor, because a 75x tile in a
dashboard is the kind of number that gets screenshotted out of context. It's in
PROGRESS.md as a known artifact for exactly that reason.

**"What would you do differently at scale?"**
Three things. First, ingestion currently accumulates one source's validated
records in memory before a batched upsert — bounded and about 23 MB at this
size, but at a hundred million touchpoints it becomes a streaming problem, so
that becomes a chunked producer/consumer. Second, attribution recomputes every
journey on every run; at scale it needs to be incremental, scored per new
conversion, with the lookback window driving invalidation. Third, the whole
thing is one Postgres — fine to about tens of millions of rows, after which
events belong in columnar storage and this becomes a dbt-style transform with
Postgres serving only the aggregates. I'd also move the pipeline off a web
process onto a real scheduler.

**"How do you know your numbers are right?"**
Invariants, not spot checks. Three that carry most of the weight. One: every
model must attribute an *identical* total — if two models disagree on the total,
that's an apportionment bug, not a difference of opinion. Two: credits sum to
exactly `Decimal("1.000000")` and attributed revenue reconciles to the cent, via
largest-remainder allocation rather than multiply-and-round; there's a test for
`$100.01` split three ways across 175 model/length/amount combinations. Three:
`received == ingested + quarantined` exactly, per source, so no record is
silently lost between the API and the database. Beyond that, ~390 tests,
idempotency asserted against a real Postgres, and the reconciliation is
re-checked end to end — total conversion revenue equals attributed revenue plus
the revenue of conversions with no touchpoints in the lookback.

**"Is the data real?"**
No, and deliberately so. It's a generator that builds a coherent world first —
users with genuine multi-touch journeys, channels biased to funnel positions,
spend reconciled against the journeys — and then serves it through fake APIs
that rate-limit, time out, return 500s and corrupt about 7% of records. Real
data would have made the pipeline easier: I wouldn't have had to handle failures
I couldn't reproduce on demand. Everything is deterministic per seed, so any bug
is reproducible.
