import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, getDemoStatus, startDemoReset } from '../api';
import { count } from '../format';

const POLL_INTERVAL_MS = 1000;
const DEMO_PAYLOAD = { seed: 42, users: 3000, failure_profile: 'normal' };

const STAGE_COPY = {
  reset: 'Truncate tables',
  ingest: 'Generate + ingest',
  replay: 'Replay quarantine',
  attribute: 'Run attribution',
};

function stageDetail(stage) {
  const detail = stage.detail;
  if (!detail) return null;
  if (stage.name === 'ingest') {
    return `${count(detail.received)} received · ${count(detail.ingested)} ingested · ${count(detail.quarantined)} quarantined · ${count(detail.retries)} retries`;
  }
  if (stage.name === 'replay') {
    return `${count(detail.recovered)} recovered · ${count(detail.still_quarantined)} still quarantined`;
  }
  if (stage.name === 'attribute') {
    return `${count(detail.journeys)} journeys · ${count(detail.rows_written)} attribution rows`;
  }
  if (stage.name === 'reset') return 'tables cleared';
  return null;
}

/**
 * The demo button: regenerates the entire dataset end to end.
 *
 * Confirms first (it destroys the current data), then polls the job until it
 * reaches a terminal state and asks the page to refetch everything.
 */
export default function RunPipeline({ onComplete }) {
  const [phase, setPhase] = useState('idle'); // idle | confirming | running | done | error
  const [job, setJob] = useState(null);
  const [message, setMessage] = useState(null);
  const timerRef = useRef(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const poll = useCallback(
    async (jobId) => {
      try {
        const status = await getDemoStatus(jobId);
        if (!mountedRef.current) return;
        setJob(status);

        if (status.status === 'running') {
          timerRef.current = setTimeout(() => poll(jobId), POLL_INTERVAL_MS);
          return;
        }

        if (status.status === 'success') {
          setPhase('done');
          setMessage('Pipeline rebuilt — refreshing every section.');
          onComplete?.();
        } else {
          setPhase('error');
          setMessage(status.error || 'The pipeline run failed.');
        }
      } catch (error) {
        if (!mountedRef.current) return;
        setPhase('error');
        setMessage(
          error instanceof ApiError
            ? `Lost contact with the job: ${error.message}`
            : 'Lost contact with the job.',
        );
      }
    },
    [onComplete],
  );

  const start = useCallback(async () => {
    setPhase('running');
    setMessage(null);
    setJob(null);
    try {
      const accepted = await startDemoReset(DEMO_PAYLOAD);
      if (!mountedRef.current) return;
      setJob(accepted);
      timerRef.current = setTimeout(
        () => poll(accepted.job_id),
        POLL_INTERVAL_MS,
      );
    } catch (error) {
      if (!mountedRef.current) return;
      // 409 is not a failure — someone (or another tab) is already running it.
      if (error instanceof ApiError && error.status === 409) {
        setPhase('idle');
        setMessage('A run is already in progress. Try again when it finishes.');
        return;
      }
      setPhase('error');
      setMessage(error?.message || 'Could not start the pipeline run.');
    }
  }, [poll]);

  const isRunning = phase === 'running';
  const stages = job?.stages ?? [];

  return (
    <div>
      <button
        type="button"
        className="btn btn-primary"
        onClick={() => setPhase(isRunning ? 'running' : 'confirming')}
        disabled={isRunning || phase === 'confirming'}
        aria-describedby={phase === 'confirming' ? 'run-confirm' : undefined}
      >
        {isRunning ? 'Running pipeline…' : 'Run pipeline'}
      </button>

      {phase === 'confirming' && (
        <div className="run-panel" id="run-confirm" role="alertdialog" aria-labelledby="run-confirm-title">
          <p className="run-panel-title" id="run-confirm-title">
            Regenerate all data?
          </p>
          <p className="run-panel-body">
            This truncates every table and rebuilds the dataset from scratch:
            generate → ingest → replay quarantine → re-run attribution. The
            numbers on this page will change. Takes about 8 seconds.
          </p>
          <div className="run-actions">
            <button type="button" className="btn btn-danger" onClick={start}>
              Yes, rebuild everything
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => setPhase('idle')}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {(isRunning || phase === 'done' || phase === 'error') && stages.length > 0 && (
        <div className="run-panel">
          <p className="run-panel-title">
            {isRunning ? 'Rebuilding dataset…' : 'Pipeline run'}
          </p>
          <ul className="stage-list">
            {stages.map((stage) => (
              <li
                className={`stage-item is-${stage.status}`}
                key={stage.name}
              >
                <span className="stage-dot" aria-hidden="true" />
                <span className="stage-name">
                  {STAGE_COPY[stage.name] || stage.name}
                </span>
                <span className="stage-detail">
                  {stage.status === 'pending'
                    ? 'waiting'
                    : stage.status === 'running'
                      ? 'working…'
                      : stageDetail(stage) || stage.status}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {message && (
        <div
          className={`banner banner-${
            phase === 'error' ? 'warn' : phase === 'done' ? 'good' : 'info'
          }`}
          role="status"
        >
          {message}
        </div>
      )}
    </div>
  );
}
