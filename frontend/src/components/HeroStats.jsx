import { count, moneyWhole, roas } from '../format';
import { modelLabel } from '../theme';
import { EmptyState, InlineError, Skeleton } from './primitives';

function Stat({ label, value, hint, compact = false }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value${compact ? ' is-compact' : ''}`}>{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

function StatSkeletons() {
  return (
    <div className="stat-strip">
      {Array.from({ length: 5 }, (_, index) => (
        <div className="stat" key={index}>
          <Skeleton height={9} width="64%" />
          <Skeleton height={26} width="82%" style={{ marginTop: 6 }} />
        </div>
      ))}
    </div>
  );
}

/**
 * The focal point of the page: one channel, valued two completely different
 * ways depending only on which attribution model you believe.
 */
function DisagreementSpotlight({ disagreement }) {
  if (!disagreement?.channel) {
    return (
      <div className="spotlight">
        <div className="spotlight-eyebrow">Model disagreement</div>
        <p className="spotlight-headline">
          No attribution results yet — run the pipeline to populate the
          dashboard.
        </p>
      </div>
    );
  }

  const { channel, high, low, swing, generousModel, stingyModel } =
    disagreement;
  const hasBothSides = high !== null && low !== null;

  return (
    <div className="spotlight">
      <div className="spotlight-eyebrow">The headline disagreement</div>

      <p className="spotlight-headline">
        <em style={{ textTransform: 'capitalize' }}>{channel}</em>
        {hasBothSides ? (
          <>
            {' '}
            is credited <em>{moneyWhole(high)}</em> under{' '}
            <em>{modelLabel(generousModel)}</em> but only{' '}
            <em>{moneyWhole(low)}</em> under <em>{modelLabel(stingyModel)}</em>{' '}
            — a <em>{moneyWhole(swing)}</em> swing.
          </>
        ) : (
          <>
            {' '}
            is the most contested channel, with a <em>{moneyWhole(swing)}</em>{' '}
            swing between <em>{modelLabel(generousModel)}</em> and{' '}
            <em>{modelLabel(stingyModel)}</em>.
          </>
        )}
      </p>

      {hasBothSides && (
        <div className="spotlight-figures">
          <div>
            <div className="spotlight-figure-label">
              {modelLabel(generousModel)}
            </div>
            <div className="spotlight-figure-value">{moneyWhole(high)}</div>
          </div>
          <div>
            <div className="spotlight-figure-label">
              {modelLabel(stingyModel)}
            </div>
            <div className="spotlight-figure-value">{moneyWhole(low)}</div>
          </div>
          <div>
            <div className="spotlight-figure-label">Contested</div>
            <div className="spotlight-figure-value is-swing">
              {moneyWhole(swing)}
            </div>
          </div>
        </div>
      )}

      <p className="spotlight-foot">
        Same conversions, same spend — only the model differs. Every model
        distributes the identical total revenue; they disagree entirely about
        who earned it.
      </p>
    </div>
  );
}

export default function HeroStats({ state, disagreement, onRetry }) {
  const { data: summary, error, isLoading, isRefreshing } = state;

  if (error && !summary) {
    return (
      <section className="section" aria-labelledby="hero-heading">
        <h2 id="hero-heading" className="sr-only">
          Headline figures
        </h2>
        <InlineError error={error} onRetry={onRetry} />
      </section>
    );
  }

  return (
    <section className="section" aria-labelledby="hero-heading">
      <h2 id="hero-heading" className="sr-only">
        Headline figures
      </h2>

      <div className={`hero${isRefreshing ? ' is-refreshing' : ''}`}>
        <div>
          {isLoading ? (
            <StatSkeletons />
          ) : !summary ? (
            <EmptyState>No summary available.</EmptyState>
          ) : (
            <div className="stat-strip">
              <Stat
                label="Attributed revenue"
                value={moneyWhole(summary.total_attributed_revenue)}
                hint="same under every model"
                compact
              />
              <Stat
                label="Ad spend"
                value={moneyWhole(summary.total_spend)}
                compact
              />
              <Stat
                label="Blended ROAS"
                value={roas(summary.blended_roas)}
                hint="revenue ÷ spend"
              />
              <Stat
                label="Conversions"
                value={count(summary.conversion_count)}
              />
              <Stat
                label="Touchpoints"
                value={count(summary.touchpoint_count)}
                hint={`${count(summary.channel_count)} channels`}
              />
            </div>
          )}
        </div>

        <div className="hero-spotlight">
          {isLoading ? (
            <div className="spotlight">
              <Skeleton height={10} width="34%" />
              <Skeleton height={64} style={{ marginTop: 16 }} />
              <Skeleton height={44} width="76%" style={{ marginTop: 20 }} />
            </div>
          ) : (
            <DisagreementSpotlight disagreement={disagreement} />
          )}
        </div>
      </div>
    </section>
  );
}
