import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, getHealth } from '../api';

/**
 * Cold-start handling for a free-tier API that sleeps when idle.
 *
 * Render spins the instance down after 15 minutes and takes 30-60s to wake.
 * During that window every request fails at the network layer, which would
 * otherwise render as "the API is down" — wrong, and it teaches a visitor to
 * give up two seconds before the dashboard would have worked.
 *
 * So the first load probes /health and reports three phases:
 *
 *   checking  — a request is in flight and has been quick so far
 *   waking    — it has taken longer than WAKE_NOTICE_MS; show the notice
 *   ready     — /health answered
 *   failed    — WAKE_TIMEOUT_MS elapsed without an answer; now it is an error
 *
 * Nothing renders the hard error state until the full window has elapsed.
 */
const WAKE_NOTICE_MS = 3000; // show the "waking up" message after this long
const WAKE_TIMEOUT_MS = 90000; // give up only after this long
const RETRY_BASE_MS = 1500;
const RETRY_MAX_MS = 8000;

export function useApiWakeup() {
  const [phase, setPhase] = useState('checking');
  const [elapsedMs, setElapsedMs] = useState(0);
  const [attempts, setAttempts] = useState(0);
  const [error, setError] = useState(null);
  const [nonce, setNonce] = useState(0);

  const cancelledRef = useRef(false);

  const retry = useCallback(() => {
    setPhase('checking');
    setElapsedMs(0);
    setAttempts(0);
    setError(null);
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    const startedAt = Date.now();
    let noticeTimer = null;
    let tickTimer = null;
    let retryTimer = null;

    // Flip to "waking" if the first probe has not returned quickly.
    noticeTimer = setTimeout(() => {
      if (!cancelledRef.current) {
        setPhase((current) => (current === 'checking' ? 'waking' : current));
      }
    }, WAKE_NOTICE_MS);

    // Drive the progress indicator.
    tickTimer = setInterval(() => {
      if (!cancelledRef.current) setElapsedMs(Date.now() - startedAt);
    }, 250);

    const attempt = async (attemptNumber) => {
      try {
        await getHealth();
        if (cancelledRef.current) return;
        setPhase('ready');
        setError(null);
      } catch (caught) {
        if (cancelledRef.current) return;
        if (caught?.name === 'AbortError') return;

        // A 4xx/5xx means the server is awake and answering — not a cold
        // start. Treat it as ready and let the sections surface their own
        // errors rather than blocking the whole page.
        if (caught instanceof ApiError && !caught.isNetworkError) {
          setPhase('ready');
          return;
        }

        const waited = Date.now() - startedAt;
        if (waited >= WAKE_TIMEOUT_MS) {
          setError(caught);
          setPhase('failed');
          return;
        }

        setPhase((current) => (current === 'ready' ? current : 'waking'));
        setAttempts(attemptNumber);
        const backoff = Math.min(
          RETRY_BASE_MS * 2 ** Math.min(attemptNumber, 3),
          RETRY_MAX_MS,
        );
        retryTimer = setTimeout(() => attempt(attemptNumber + 1), backoff);
      }
    };

    attempt(1);

    return () => {
      cancelledRef.current = true;
      clearTimeout(noticeTimer);
      clearInterval(tickTimer);
      clearTimeout(retryTimer);
    };
  }, [nonce]);

  return {
    phase,
    elapsedMs,
    attempts,
    error,
    retry,
    isReady: phase === 'ready',
    timeoutMs: WAKE_TIMEOUT_MS,
  };
}

export { WAKE_NOTICE_MS, WAKE_TIMEOUT_MS };
