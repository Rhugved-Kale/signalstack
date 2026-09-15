/**
 * Colour palette — defined once, used everywhere.
 *
 * Two categorical scales with two different jobs:
 *
 *   CHANNEL_COLORS — identity of a marketing channel. A channel keeps its hue
 *     in every chart, table swatch and journey chain, so "display is blue" is
 *     learned once. Bars re-order when the model changes; the colours do not
 *     follow rank.
 *
 *   MODEL_COLORS — identity of an attribution model. Used only in the model
 *     comparison chart, where the *series* is the model and the channel is
 *     carried by the axis label instead.
 *
 * Both scales are taken in fixed slot order from a palette validated for
 * colour-vision deficiency and lightness/chroma banding in light and dark
 * mode (worst adjacent CVD ΔE 9.1 light / 8.4 dark; normal-vision ΔE 19.6 /
 * 19.3). Three light-mode hues sit under 3:1 contrast against the surface, so
 * every chart ships a table view and direct value labels as relief.
 *
 * Never generate or cycle a hue for a 7th series — fold it into "other".
 */

// Funnel order: early → late → unattributed. Slots 1..6.
export const CHANNEL_ORDER = [
  'display',
  'social',
  'affiliate',
  'email',
  'paid_search',
  'organic',
];

const CHANNEL_LIGHT = {
  display: '#2a78d6',
  social: '#eb6834',
  affiliate: '#1baf7a',
  email: '#eda100',
  paid_search: '#e87ba4',
  organic: '#008300',
};

const CHANNEL_DARK = {
  display: '#3987e5',
  social: '#d95926',
  affiliate: '#199e70',
  email: '#c98500',
  paid_search: '#d55181',
  organic: '#008300',
};

// Ordered early-funnel-credit → late-funnel-credit, so the grouped bars read
// as a gradient of where each model places the credit. Slots 1..5.
export const MODEL_ORDER = [
  'first_touch',
  'position_based',
  'linear',
  'time_decay',
  'last_touch',
];

const MODEL_LIGHT = {
  first_touch: '#2a78d6',
  position_based: '#eb6834',
  linear: '#1baf7a',
  time_decay: '#eda100',
  last_touch: '#e87ba4',
};

const MODEL_DARK = {
  first_touch: '#3987e5',
  position_based: '#d95926',
  linear: '#199e70',
  time_decay: '#c98500',
  last_touch: '#d55181',
};

const FALLBACK_LIGHT = '#8a8880';
const FALLBACK_DARK = '#6f6e68';

export function palette(isDark) {
  return {
    channel: isDark ? CHANNEL_DARK : CHANNEL_LIGHT,
    model: isDark ? MODEL_DARK : MODEL_LIGHT,
    fallback: isDark ? FALLBACK_DARK : FALLBACK_LIGHT,
    // Recessive chart chrome: one shade off the surface, solid hairlines.
    grid: isDark ? '#2e2e2c' : '#ebeae5',
    axis: isDark ? '#8a8880' : '#8a8880',
    surface: isDark ? '#1a1a19' : '#fcfcfb',
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
