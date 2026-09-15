/**
 * Colour palette — defined once, used everywhere.
 *
 * Two scales with two different jobs:
 *
 *   CHANNEL_COLORS — identity of a marketing channel. A channel keeps its hue
 *     in every chart, table swatch and journey chain, so "display is indigo"
 *     is learned once. Bars re-order when the model changes; the colours do
 *     not follow rank. (Hard requirement carried over from Phase 7.)
 *
 *   MODEL_RAMP — the five models are *ordered*, from crediting the top of the
 *     funnel to crediting the bottom, so they get a single-hue ordinal ramp
 *     rather than five competing hues. The comparison chart then reads as a
 *     spectrum, which is both quieter and a better match for the data.
 *
 * Every set below was generated in OKLCH at a deliberately muted chroma
 * (~0.11-0.12, against the previous 0.15-0.20) and validated with the data-viz
 * skill's checker. Light and dark are separately stepped for their own
 * surface, never dimmed copies of each other:
 *
 *   channels, light (surface #fbfaf8): all checks PASS, 0 warnings
 *     worst adjacent CVD ΔE 8.8 (deutan) · normal-vision ΔE 18.6 · all >= 3:1
 *   channels, dark (surface #141413): all checks PASS, 0 warnings
 *     worst adjacent CVD ΔE 8.3 (protan) · normal-vision ΔE 18.0 · all >= 3:1
 *   model ramp, both modes (--ordinal): all checks PASS
 *     monotone lightness, adjacent ΔL >= 0.06, light end clears 2:1
 *
 * Caveat worth knowing: no six-colour categorical palette clears the
 * *all-pairs* gates (the skill states this is structural past three slots),
 * and the horizontal bars sort by revenue so any two channels can end up
 * adjacent. Colour is therefore never the sole identity channel — every chart
 * labels the channel on the axis, direct-labels its value, and ships a table
 * view.
 */

// Funnel order: early -> late -> unattributed.
export const CHANNEL_ORDER = [
  'display',
  'social',
  'affiliate',
  'email',
  'paid_search',
  'organic',
];

const CHANNEL_LIGHT = {
  display: '#4a7dbf', // dusty indigo
  social: '#bf6954', // terracotta
  affiliate: '#00958f', // teal
  email: '#b58832', // ochre
  paid_search: '#a6567e', // plum
  organic: '#559758', // sage
};

const CHANNEL_DARK = {
  display: '#5688c8',
  social: '#c8735e',
  affiliate: '#009a94',
  email: '#b68a38',
  paid_search: '#a95b82',
  organic: '#65a668',
};

// Ordered so the ramp runs top-of-funnel credit -> bottom-of-funnel credit.
export const MODEL_ORDER = [
  'first_touch',
  'position_based',
  'linear',
  'time_decay',
  'last_touch',
];

// Single hue (262°), monotone lightness, light -> dark.
const MODEL_LIGHT = {
  first_touch: '#8caadf',
  position_based: '#7794c8',
  linear: '#637fb2',
  time_decay: '#4e699a',
  last_touch: '#385281',
};

// Dark mode inverts the direction so the ramp stays legible on a dark surface.
const MODEL_DARK = {
  first_touch: '#4b6493',
  position_based: '#607aaa',
  linear: '#7691c2',
  time_decay: '#8ca8db',
  last_touch: '#a3c0f4',
};

const FALLBACK_LIGHT = '#8d8980';
const FALLBACK_DARK = '#7c786f';

export function palette(isDark) {
  return {
    channel: isDark ? CHANNEL_DARK : CHANNEL_LIGHT,
    model: isDark ? MODEL_DARK : MODEL_LIGHT,
    fallback: isDark ? FALLBACK_DARK : FALLBACK_LIGHT,
    // Chart chrome sits one step off the surface: hairlines, never boxes.
    grid: isDark ? '#22201d' : '#f2f0ec',
    axis: isDark ? '#83807b' : '#716e6a',
    surface: isDark ? '#141413' : '#fbfaf8',
  };
}

/** Human-readable channel label. */
export function channelLabel(channel) {
  return channel.replace(/_/g, ' ');
}

/** Human-readable model label. */
export function modelLabel(model) {
  return model.replace(/_/g, '-');
}
