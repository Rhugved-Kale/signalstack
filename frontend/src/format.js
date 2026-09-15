/**
 * Number formatting. Every figure on the page goes through one of these —
 * raw floats like 42607.510000000002 must never reach the DOM.
 */

const MONEY = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const MONEY_WHOLE = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 0,
});

const COUNT = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

const DECIMAL_1 = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

const EM_DASH = '—';

function isNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

/** $1,234.56 — or an em dash for null/undefined/NaN/Infinity. */
export function money(value) {
  return isNumber(value) ? MONEY.format(value) : EM_DASH;
}

/** $1,235 — for tight spaces where cents are noise. */
export function moneyWhole(value) {
  return isNumber(value) ? MONEY_WHOLE.format(value) : EM_DASH;
}

/** $42.6k — for axis ticks. */
export function moneyCompact(value) {
  if (!isNumber(value)) return EM_DASH;
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `$${DECIMAL_1.format(value / 1_000_000)}M`;
  if (abs >= 1_000) return `$${DECIMAL_1.format(value / 1_000)}k`;
  return MONEY_WHOLE.format(value);
}

/** 7,879 */
export function count(value) {
  return isNumber(value) ? COUNT.format(value) : EM_DASH;
}

/** 233.0 — attributed conversions are fractional by design. */
export function conversions(value) {
  return isNumber(value) ? DECIMAL_1.format(value) : EM_DASH;
}

/**
 * 7.81× — or an em dash. Organic has no spend, so its ROAS is genuinely
 * null; it must never render as NaN, Infinity or 0.00×.
 */
export function roas(value) {
  return isNumber(value) ? `${DECIMAL_1.format(value)}×` : EM_DASH;
}

/** 16.7% — from a 0..1 credit fraction. */
export function creditPercent(value) {
  return isNumber(value) ? `${DECIMAL_1.format(value * 100)}%` : EM_DASH;
}

/** 20 Jul 2026 */
export function shortDate(value) {
  if (!value) return EM_DASH;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return EM_DASH;
  return parsed.toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  });
}

/** 14:32:07 */
export function clockTime(value) {
  if (!value) return EM_DASH;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return EM_DASH;
  return parsed.toLocaleTimeString('en-GB', { hour12: false });
}

/** 1.4s */
export function duration(startedAt, finishedAt) {
  if (!startedAt || !finishedAt) return EM_DASH;
  const ms = new Date(finishedAt) - new Date(startedAt);
  if (!Number.isFinite(ms) || ms < 0) return EM_DASH;
  return `${DECIMAL_1.format(ms / 1000)}s`;
}

export { EM_DASH };
