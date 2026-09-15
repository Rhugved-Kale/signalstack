import { BASE_URL, IS_LOCAL_API } from '../api';

/**
 * The cold-start screen. Shown instead of the dashboard while the API wakes,
 * and instead of an error until the retry window has fully elapsed.
 */
export function ApiWaking({ elapsedMs, timeoutMs, attempts }) {
  const seconds = Math.floor(elapsedMs / 1000);
  const pct = Math.min(100, (elapsedMs / timeoutMs) * 100);

  return (
    <main className="page">
      <div className="page-error" role="status" aria-live="polite">
        <h1 className="page-error-title">Waking up the API</h1>
        <p className="page-error-body">
          Free hosting sleeps when idle, this takes up to a minute.
        </p>

        <div
          className="wake-progress"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={Math.round(timeoutMs / 1000)}
          aria-valuenow={seconds}
          aria-label="Time spent waiting for the API"
        >
          <div className="wake-progress-bar" style={{ width: `${pct}%` }} />
        </div>

        <p className="page-error-body" style={{ fontSize: 12.5 }}>
          {seconds}s elapsed
          {attempts > 1 ? ` · retry ${attempts}` : ''} · still trying
        </p>
      </div>
    </main>
  );
}

/** The hard error, only after the wake-up window has elapsed. */
export function ApiUnreachable({ error, onRetry }) {
  return (
    <main className="page">
      <div className="page-error" role="alert">
        <h1 className="page-error-title">Could not reach the SignalStack API</h1>
        <p className="page-error-body">
          Nothing answered at <code>{BASE_URL || '(VITE_API_URL is unset)'}</code>{' '}
          after 90 seconds, so it is probably not just a cold start.
        </p>
        {IS_LOCAL_API ? (
          <p className="page-error-body">
            Running locally? Start it with{' '}
            <code>cd backend &amp;&amp; ./start.sh</code> and make sure Postgres
            is up (<code>docker compose up -d</code>).
          </p>
        ) : (
          <p className="page-error-body">
            The API runs on free hosting, which sleeps when idle and can take up
            to a minute to wake. Waiting a moment and retrying usually fixes it.
            If it keeps failing, the service may be redeploying — try again in a
            few minutes.
          </p>
        )}
        {error?.detail && (
          <p className="page-error-body" style={{ fontSize: 12.5 }}>
            {error.detail}
          </p>
        )}
        <button
          type="button"
          className="btn btn-primary"
          onClick={onRetry}
          style={{ marginTop: 18 }}
        >
          Try again
        </button>
      </div>
    </main>
  );
}
