/**
 * Small pure derivations over API responses.
 *
 * The API's /api/summary reports *which* channel the models disagree about
 * most and by how much, but not the two revenue figures either side of that
 * swing — those live in /api/model-comparison. The spotlight wants all four,
 * so it is assembled here rather than in a component.
 */

/**
 * @returns {{channel, high, low, swing, generousModel, stingyModel}|null}
 */
export function buildDisagreement(summary, comparison) {
  const fromSummary = summary?.largest_disagreement;
  const rows = comparison?.channels ?? [];

  // /api/model-comparison returns channels already sorted by swing desc.
  const row =
    (fromSummary?.channel &&
      rows.find((entry) => entry.channel === fromSummary.channel)) ||
    rows[0] ||
    null;

  if (!row && !fromSummary?.channel) return null;

  if (!row) {
    // Comparison not loaded yet: show what the summary alone can tell us.
    return {
      channel: fromSummary.channel,
      high: null,
      low: null,
      swing: fromSummary.swing ?? null,
      generousModel: fromSummary.most_generous_model ?? null,
      stingyModel: fromSummary.least_generous_model ?? null,
    };
  }

  return {
    channel: row.channel,
    high: row.max_revenue,
    low: row.min_revenue,
    swing: row.swing,
    generousModel: row.most_generous_model,
    stingyModel: row.least_generous_model,
  };
}

/** Rows for the grouped comparison chart, one object per channel. */
export function comparisonChartRows(comparison, modelOrder) {
  const rows = comparison?.channels ?? [];
  return rows.map((row) => {
    const point = { channel: row.channel, swing: row.swing };
    for (const model of modelOrder) {
      point[model] = row.by_model?.[model] ?? 0;
    }
    return point;
  });
}

/** Total quarantined records, tolerating a missing payload. */
export function quarantineTotal(health) {
  return health?.quarantine?.total ?? 0;
}

/**
 * Models that credit every touchpoint in a journey. Their /api/journeys rows
 * therefore describe the *complete* path.
 */
export const MULTI_TOUCH_MODELS = new Set([
  'linear',
  'time_decay',
  'position_based',
]);

export const PATH_SOURCE_MODEL = 'linear';

/**
 * Build the journey list for the explorer.
 *
 * /api/journeys returns only the touchpoints that *received credit* under the
 * requested model. Under `last_touch` or `first_touch` that is a single
 * touchpoint, so the customer's path would collapse to one node and the panel
 * would lose its point.
 *
 * So for single-touch models the path comes from `linear` (which credits every
 * touchpoint, making its rows the full journey) and the selected model's
 * credits are overlaid on top. A touchpoint the selected model ignored shows
 * 0% — which is exactly what "last touch takes everything" looks like, and is
 * the most direct way to see the model at work.
 */
export function buildJourneyList(model, selectedJourneys, pathJourneys, limit = 12) {
  const selected = selectedJourneys?.data ?? [];

  if (MULTI_TOUCH_MODELS.has(model)) {
    return { journeys: selected.slice(0, limit), isOverlaid: false };
  }

  const paths = pathJourneys?.data ?? [];
  if (!paths.length) {
    // Path source not loaded yet — show what we have rather than nothing.
    return { journeys: selected.slice(0, limit), isOverlaid: false };
  }

  // conversion_id -> touchpoint_id -> credited row, under the selected model.
  const creditIndex = new Map();
  for (const journey of selected) {
    const byTouchpoint = new Map();
    for (const touch of journey.touchpoints ?? []) {
      byTouchpoint.set(touch.touchpoint_id, touch);
    }
    creditIndex.set(journey.conversion_id, byTouchpoint);
  }

  const merged = [];
  for (const path of paths) {
    const credits = creditIndex.get(path.conversion_id);
    // Only journeys we have credits for under the selected model.
    if (!credits) continue;

    merged.push({
      ...path,
      touchpoints: (path.touchpoints ?? []).map((touch) => {
        const credited = credits.get(touch.touchpoint_id);
        return {
          ...touch,
          credit: credited ? credited.credit : 0,
          attributed_revenue: credited ? credited.attributed_revenue : 0,
        };
      }),
    });
    if (merged.length >= limit) break;
  }

  return { journeys: merged, isOverlaid: true };
}
