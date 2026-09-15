/** Small shared building blocks: cards, skeletons, states, badges. */

/**
 * A region of the page, separated by a hairline rule rather than drawn as a
 * bordered card with a shadow. Same API as before so callers are unchanged.
 */
export function Card({ title, subtitle, action, children, className = '' }) {
  return (
    <section className={`panel ${className}`.trim()}>
      {(title || action) && (
        <header
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: 'var(--s3)',
            marginBottom: 'var(--s4)',
          }}
        >
          <div>
            {title && <h3 className="panel-title">{title}</h3>}
            {subtitle && <p className="panel-subtitle">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Skeleton({ height = 16, width = '100%', radius = 6, style }) {
  return (
    <div
      className="skeleton"
      aria-hidden="true"
      style={{ height, width, borderRadius: radius, ...style }}
    />
  );
}

export function SkeletonLines({ count = 3, height = 14, gap = 9 }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap }}>
      {Array.from({ length: count }, (_, index) => (
        <Skeleton
          key={index}
          height={height}
          width={`${100 - index * 9}%`}
        />
      ))}
    </div>
  );
}

/**
 * A per-section failure. The page-level "API unreachable" message is handled
 * once in App, so this only ever renders for an endpoint-specific problem.
 */
export function InlineError({ error, onRetry }) {
  return (
    <div className="inline-error" role="status">
      <strong>Could not load this section.</strong>{' '}
      {error?.message || 'Unexpected error.'}
      {onRetry && (
        <>
          {' '}
          <button type="button" className="btn" onClick={onRetry} style={{ marginTop: 8 }}>
            Retry
          </button>
        </>
      )}
    </div>
  );
}

export function EmptyState({ children }) {
  return (
    <p className="empty-state" role="status">
      {children}
    </p>
  );
}

export function Badge({ tone = 'info', children }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

/** Maps an ingestion-run status onto a badge tone. */
export function statusTone(status) {
  if (status === 'success') return 'good';
  if (status === 'failed') return 'warn';
  return 'info';
}
