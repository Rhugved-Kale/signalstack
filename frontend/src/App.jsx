import { useCallback, useMemo, useState } from 'react';

import {
  BASE_URL,
  getChannels,
  getJourneys,
  getModelComparison,
  getModels,
  getPipelineHealth,
  getSummary,
} from './api';
import { shortDate } from './format';
import { palette } from './theme';
import {
  MULTI_TOUCH_MODELS,
  PATH_SOURCE_MODEL,
  buildDisagreement,
  buildJourneyList,
} from './selectors';
import { useApi } from './hooks/useApi';
import { usePrefersDark } from './hooks/usePrefersDark';

import ChannelPerformance from './components/ChannelPerformance';
import HeroStats from './components/HeroStats';
import JourneyExplorer from './components/JourneyExplorer';
import ModelComparison from './components/ModelComparison';
import ModelSelector from './components/ModelSelector';
import PipelineHealth from './components/PipelineHealth';
import RunPipeline from './components/RunPipeline';

const DEFAULT_MODEL = 'last_touch';
const JOURNEY_LIMIT = 12;
// Fetched wide so a single-touch model's credits can be matched onto the full
// paths sourced from `linear`.
const JOURNEY_FETCH_LIMIT = 100;

/**
 * Shown when the API cannot be reached at all. One page-level message, not
 * five section-level error boxes.
 */
function ApiUnreachable({ error, onRetry }) {
  return (
    <main className="page">
      <div className="page-error" role="alert">
        <h1 className="page-error-title">Waiting for the SignalStack API</h1>
        <p className="page-error-body">
          The dashboard could not reach <code>{BASE_URL || '(unset)'}</code>.
          The API may still be starting up.
        </p>
        <p className="page-error-body">
          Start it with{' '}
          <code>cd backend &amp;&amp; .venv/bin/uvicorn app.main:app --port 8000</code>{' '}
          and make sure Postgres is running (<code>docker compose up -d</code>).
        </p>
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
          Retry connection
        </button>
      </div>
    </main>
  );
}

export default function App() {
  const [model, setModel] = useState(DEFAULT_MODEL);
  // Bumping this re-runs every fetch on the page.
  const [refreshKey, setRefreshKey] = useState(0);

  const isDark = usePrefersDark();
  const colors = useMemo(() => palette(isDark), [isDark]);

  const refreshAll = useCallback(() => {
    setRefreshKey((key) => key + 1);
  }, []);

  // The bootstrap call. If this fails with a network error the whole page
  // shows one message instead of every section failing separately.
  const modelsState = useApi(
    (signal) => getModels(signal),
    [refreshKey],
  );

  const summaryState = useApi(
    (signal) => getSummary(model, signal),
    [model, refreshKey],
  );
  const channelsState = useApi(
    (signal) => getChannels(model, signal),
    [model, refreshKey],
  );
  const comparisonState = useApi(
    (signal) => getModelComparison(signal),
    [refreshKey],
  );
  const journeysState = useApi(
    (signal) => getJourneys(model, JOURNEY_FETCH_LIMIT, signal),
    [model, refreshKey],
  );
  // Single-touch models credit one touchpoint, so their rows do not describe
  // the customer's path. `linear` credits every touchpoint, so it does.
  const needsPathSource = !MULTI_TOUCH_MODELS.has(model);
  const journeyPathsState = useApi(
    (signal) => getJourneys(PATH_SOURCE_MODEL, JOURNEY_FETCH_LIMIT, signal),
    [refreshKey],
    { enabled: needsPathSource },
  );
  const healthState = useApi(
    (signal) => getPipelineHealth(signal),
    [refreshKey],
  );

  const models = modelsState.data?.models ?? [];

  const disagreement = useMemo(
    () => buildDisagreement(summaryState.data, comparisonState.data),
    [summaryState.data, comparisonState.data],
  );

  const { journeys, isOverlaid } = useMemo(
    () =>
      buildJourneyList(
        model,
        journeysState.data,
        journeyPathsState.data,
        JOURNEY_LIMIT,
      ),
    [model, journeysState.data, journeyPathsState.data],
  );

  // Treat a network-level failure on any core call as "API is down".
  const networkError = [modelsState, summaryState, channelsState].find(
    (state) => state.error?.isNetworkError,
  )?.error;

  if (networkError) {
    return <ApiUnreachable error={networkError} onRetry={refreshAll} />;
  }

  const range = summaryState.data?.date_range;

  return (
    <main className="page">
      <header className="masthead">
        <div>
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">
              S
            </span>
            <h1 className="brand-name">SignalStack</h1>
          </div>
          <p className="tagline">
            Which marketing channel actually drove this revenue?
          </p>
        </div>

        <div className="masthead-meta">
          <div className="meta-block">
            <span className="meta-label">Data covering</span>
            <span className="meta-value">
              {range?.start ? (
                <>
                  {shortDate(range.start)} — {shortDate(range.end)}
                </>
              ) : (
                '—'
              )}
            </span>
          </div>
          <RunPipeline onComplete={refreshAll} />
        </div>
      </header>

      <div className="control-bar">
        <ModelSelector
          models={models}
          value={model}
          onChange={setModel}
          isLoading={modelsState.isLoading}
        />
        <p className="section-note" style={{ maxWidth: '46ch' }}>
          Changing the model re-scores every conversion&rsquo;s journey. Section
          3 compares all five at once.
        </p>
      </div>

      <HeroStats
        state={summaryState}
        disagreement={disagreement}
        onRetry={refreshAll}
      />

      <ChannelPerformance
        state={channelsState}
        model={model}
        colors={colors}
        onRetry={refreshAll}
      />

      <ModelComparison
        state={comparisonState}
        colors={colors}
        onRetry={refreshAll}
      />

      <section className="section" aria-labelledby="explorer-heading">
        <div className="section-head">
          <h2 id="explorer-heading" className="section-title">
            Section 4 · Journeys &amp; pipeline
          </h2>
          <p className="section-note">
            The concrete version of everything above: one customer&rsquo;s path,
            and the data-quality work behind the numbers.
          </p>
        </div>

        <div className="split">
          <JourneyExplorer
            state={journeysState}
            journeys={journeys}
            isOverlaid={isOverlaid}
            model={model}
            colors={colors}
            onRetry={refreshAll}
          />
          <PipelineHealth state={healthState} onRetry={refreshAll} />
        </div>
      </section>

      <footer
        style={{
          marginTop: 34,
          paddingTop: 16,
          borderTop: '1px solid var(--border)',
          fontSize: 12.5,
          color: 'var(--text-3)',
        }}
      >
        SignalStack — synthetic data, five attribution models, one contested
        question. API at <code>{BASE_URL}</code>.
      </footer>
    </main>
  );
}
