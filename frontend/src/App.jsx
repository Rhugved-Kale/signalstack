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
import { useApiWakeup } from './hooks/useApiWakeup';
import { usePrefersDark } from './hooks/usePrefersDark';

import { ApiUnreachable, ApiWaking } from './components/ApiWakeup';

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

export default function App() {
  const [model, setModel] = useState(DEFAULT_MODEL);
  // Bumping this re-runs every fetch on the page.
  const [refreshKey, setRefreshKey] = useState(0);

  const isDark = usePrefersDark();
  const colors = useMemo(() => palette(isDark), [isDark]);

  // Probe the API before mounting the dashboard. On a free tier the first
  // request can take a minute; the sections below only fetch once it answers,
  // so a cold start shows one progress screen instead of six failures.
  const wakeup = useApiWakeup();

  const refreshAll = useCallback(() => {
    setRefreshKey((key) => key + 1);
  }, []);

  // None of these fire until the wake-up probe succeeds, so a sleeping API
  // produces one progress screen rather than six simultaneous failures.
  const modelsState = useApi(
    (signal) => getModels(signal),
    [refreshKey],
    { enabled: wakeup.isReady },
  );

  const summaryState = useApi(
    (signal) => getSummary(model, signal),
    [model, refreshKey],
    { enabled: wakeup.isReady },
  );
  const channelsState = useApi(
    (signal) => getChannels(model, signal),
    [model, refreshKey],
    { enabled: wakeup.isReady },
  );
  const comparisonState = useApi(
    (signal) => getModelComparison(signal),
    [refreshKey],
    { enabled: wakeup.isReady },
  );
  const journeysState = useApi(
    (signal) => getJourneys(model, JOURNEY_FETCH_LIMIT, signal),
    [model, refreshKey],
    { enabled: wakeup.isReady },
  );
  // Single-touch models credit one touchpoint, so their rows do not describe
  // the customer's path. `linear` credits every touchpoint, so it does.
  const needsPathSource = !MULTI_TOUCH_MODELS.has(model);
  const journeyPathsState = useApi(
    (signal) => getJourneys(PATH_SOURCE_MODEL, JOURNEY_FETCH_LIMIT, signal),
    [refreshKey],
    { enabled: needsPathSource && wakeup.isReady },
  );
  const healthState = useApi(
    (signal) => getPipelineHealth(signal),
    [refreshKey],
    { enabled: wakeup.isReady },
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

  // Cold start: one progress screen, never six section errors.
  if (wakeup.phase === 'checking' || wakeup.phase === 'waking') {
    return (
      <ApiWaking
        elapsedMs={wakeup.elapsedMs}
        timeoutMs={wakeup.timeoutMs}
        attempts={wakeup.attempts}
      />
    );
  }

  // Only after the full retry window has elapsed is this a real failure.
  if (wakeup.phase === 'failed') {
    return <ApiUnreachable error={wakeup.error} onRetry={wakeup.retry} />;
  }

  // The API answered but a core call still failed at the network layer (it
  // went back to sleep mid-session, say) — re-probe rather than guess.
  const networkError = [modelsState, summaryState, channelsState].find(
    (state) => state.error?.isNetworkError,
  )?.error;

  if (networkError) {
    return <ApiUnreachable error={networkError} onRetry={wakeup.retry} />;
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
