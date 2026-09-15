import { useEffect, useState } from 'react';

import { creditPercent, money } from '../format';
import { channelLabel } from '../theme';
import { Card, EmptyState, InlineError, Skeleton } from './primitives';

function Chain({ touchpoints, colors, max = 6 }) {
  const shown = touchpoints.slice(0, max);
  const overflow = touchpoints.length - shown.length;

  return (
    <span className="journey-chain">
      {shown.map((touch, index) => (
        <span className="chain-node" key={touch.touchpoint_id}>
          <span
            className="chain-dot"
            style={{
              background: colors.channel[touch.channel] || colors.fallback,
            }}
            aria-hidden="true"
          />
          {channelLabel(touch.channel)}
          {index < shown.length - 1 && (
            <span className="chain-arrow" aria-hidden="true">
              &nbsp;→
            </span>
          )}
        </span>
      ))}
      {overflow > 0 && (
        <span className="chain-node" style={{ color: 'var(--text-3)' }}>
          +{overflow} more
        </span>
      )}
    </span>
  );
}

function JourneyRow({ journey, colors, isOpen, onToggle }) {
  const panelId = `journey-panel-${journey.conversion_id}`;
  const touchpoints = journey.touchpoints ?? [];
  const maxCredit = touchpoints.reduce(
    (best, touch) => Math.max(best, Number(touch.credit) || 0),
    0,
  );

  return (
    <li className={`journey-row${isOpen ? ' is-open' : ''}`}>
      {/* A real <button> so it is keyboard-operable and announced correctly. */}
      <button
        type="button"
        className="journey-trigger"
        aria-expanded={isOpen}
        aria-controls={panelId}
        onClick={onToggle}
      >
        <span className="journey-caret" aria-hidden="true">
          ▶
        </span>
        <span className="journey-id">{journey.user_id}</span>
        <Chain touchpoints={touchpoints} colors={colors} />
        <span className="journey-revenue">{money(journey.revenue)}</span>
      </button>

      {isOpen && (
        <div className="journey-detail" id={panelId}>
          <p
            className="card-subtitle"
            style={{ marginBottom: 6, marginTop: 4 }}
          >
            {touchpoints.length} touchpoints ·{' '}
            {money(journey.revenue)} split by credit
          </p>
          {touchpoints.map((touch) => {
            const credit = Number(touch.credit) || 0;
            const width = maxCredit > 0 ? (credit / maxCredit) * 100 : 0;
            const color = colors.channel[touch.channel] || colors.fallback;
            return (
              <div className="credit-row" key={touch.touchpoint_id}>
                <span
                  className="chain-dot"
                  style={{ background: color }}
                  aria-hidden="true"
                />
                <span style={{ textTransform: 'capitalize' }}>
                  {channelLabel(touch.channel)}
                </span>
                <span className="credit-bar-track">
                  <span
                    className="credit-bar-fill"
                    style={{ width: `${width}%`, background: color }}
                  />
                </span>
                <span
                  className="credit-pct"
                  style={credit === 0 ? { color: 'var(--text-3)' } : undefined}
                >
                  {creditPercent(credit)}
                </span>
                <span
                  className="credit-money"
                  style={credit === 0 ? { color: 'var(--text-3)' } : undefined}
                >
                  {money(touch.attributed_revenue)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </li>
  );
}

export default function JourneyExplorer({
  state,
  journeys,
  isOverlaid,
  model,
  colors,
  onRetry,
}) {
  const { error, isLoading, isRefreshing } = state;
  const [openId, setOpenId] = useState(null);

  // Collapse when the model changes — the credits on screen would otherwise
  // belong to the previous model for a frame.
  useEffect(() => {
    setOpenId(null);
  }, [model]);

  return (
    <Card
      title="Journey explorer"
      subtitle={
        isOverlaid
          ? `One customer's full path. Expand a row to see ${model} hand 100% to a single touch and nothing to the rest.`
          : `One customer's path at a time. Expand a row to see how ${model} splits that order.`
      }
    >
      {error && !journeys.length ? (
        <InlineError error={error} onRetry={onRetry} />
      ) : isLoading ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {Array.from({ length: 6 }, (_, index) => (
            <Skeleton key={index} height={46} radius={8} />
          ))}
        </div>
      ) : !journeys.length ? (
        <EmptyState>No journeys yet. Run the pipeline to populate them.</EmptyState>
      ) : (
        <ul
          className={`journey-list${isRefreshing ? ' is-refreshing' : ''}`}
        >
          {journeys.map((journey) => (
            <JourneyRow
              key={journey.conversion_id}
              journey={journey}
              colors={colors}
              isOpen={openId === journey.conversion_id}
              onToggle={() =>
                setOpenId((current) =>
                  current === journey.conversion_id
                    ? null
                    : journey.conversion_id,
                )
              }
            />
          ))}
        </ul>
      )}
    </Card>
  );
}
