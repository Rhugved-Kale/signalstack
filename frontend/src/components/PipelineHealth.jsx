import { count, duration } from '../format';
import { Badge, Card, EmptyState, InlineError, Skeleton, statusTone } from './primitives';

const TABLE_ORDER = [
  ['campaigns', 'Campaigns'],
  ['ad_spend', 'Ad spend'],
  ['touchpoints', 'Touchpoints'],
  ['conversions', 'Conversions'],
  ['attribution_results', 'Attribution rows'],
  ['quarantined_records', 'Quarantined'],
];

export default function PipelineHealth({ state, onRetry }) {
  const { data, error, isLoading, isRefreshing } = state;
  const runs = data?.recent_runs ?? [];
  const rowCounts = data?.row_counts ?? {};
  const quarantine = data?.quarantine ?? { total: 0, by_source: {}, by_reason: [] };
  const topReasons = (quarantine.by_reason ?? []).slice(0, 5);
  const received = runs.reduce((sum, run) => sum + (run.records_received || 0), 0);
  const caughtPct =
    received > 0 ? ((quarantine.total / received) * 100).toFixed(1) : null;

  return (
    <Card
      title="Pipeline health"
      subtitle="Ingestion is built to expect broken upstream data. Quarantined rows are records the validator caught — not failures."
    >
      {error && !data ? (
        <InlineError error={error} onRetry={onRetry} />
      ) : isLoading ? (
        <>
          <Skeleton height={62} />
          <Skeleton height={130} style={{ marginTop: 14 }} />
        </>
      ) : !data ? (
        <EmptyState>No pipeline data available.</EmptyState>
      ) : (
        <div className={isRefreshing ? 'is-refreshing' : undefined}>
          <div className="health-strip">
            {TABLE_ORDER.map(([key, label]) => (
              <div className="health-tile" key={key}>
                <div className="health-tile-label">{label}</div>
                <div className="health-tile-value">{count(rowCounts[key])}</div>
              </div>
            ))}
          </div>

          <h4 className="subhead">Recent ingestion runs</h4>
          {!runs.length ? (
            <EmptyState>No runs recorded yet.</EmptyState>
          ) : (
            <div className="table-wrap" style={{ marginTop: 0 }}>
              <table className="data">
                <thead>
                  <tr>
                    <th scope="col">Source</th>
                    <th scope="col">Status</th>
                    <th scope="col">Received</th>
                    <th scope="col">Ingested</th>
                    <th scope="col">Quarantined</th>
                    <th scope="col">Retries</th>
                    <th scope="col">Took</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.slice(0, 6).map((run) => (
                    <tr key={run.id}>
                      <td style={{ textTransform: 'capitalize' }}>
                        {run.source.replace(/_/g, ' ')}
                      </td>
                      <td>
                        <Badge tone={statusTone(run.status)}>{run.status}</Badge>
                      </td>
                      <td className="num">{count(run.records_received)}</td>
                      <td className="num">{count(run.records_ingested)}</td>
                      <td className="num">{count(run.records_quarantined)}</td>
                      <td className="num">{count(run.retry_count)}</td>
                      <td className="num">
                        {duration(run.started_at, run.finished_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h4 className="subhead">
            Quarantine · {count(quarantine.total)} records caught
          </h4>
          {!topReasons.length ? (
            <EmptyState>Nothing quarantined — every record validated.</EmptyState>
          ) : (
            <ul className="reason-list">
              {topReasons.map((reason) => (
                <li
                  className="reason-item"
                  key={`${reason.source}-${reason.error_reason}`}
                >
                  <span className="reason-count">{count(reason.count)}</span>
                  <span className="reason-text">
                    <span className="reason-source">{reason.source}</span>{' '}
                    {reason.error_reason}
                  </span>
                </li>
              ))}
            </ul>
          )}

          <p className="note-good">
            {caughtPct
              ? `${caughtPct}% of received records were rejected and quarantined with the raw payload kept for replay. `
              : ''}
            Malformed records never reach the warehouse, and a bad record never
            aborts a run — this is the validation layer working, not an error
            state.
          </p>
        </div>
      )}
    </Card>
  );
}
