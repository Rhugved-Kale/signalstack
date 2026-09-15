import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Fetch-on-mount with loading / error / empty handling.
 *
 * On a refetch (model change, pipeline re-run) the previous data is *kept* and
 * `isRefreshing` goes true, so sections fade rather than collapsing into
 * skeletons and the page never jumps.
 */
export function useApi(fetcher, deps = [], { enabled = true } = {}) {
  const [state, setState] = useState({
    data: null,
    error: null,
    isLoading: enabled,
    isRefreshing: false,
  });

  const hasLoadedRef = useRef(false);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const runFetch = useCallback(fetcher, deps);

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, isLoading: false, isRefreshing: false });
      return undefined;
    }

    const controller = new AbortController();
    let active = true;

    setState((previous) => ({
      ...previous,
      isLoading: !hasLoadedRef.current,
      isRefreshing: hasLoadedRef.current,
      error: null,
    }));

    runFetch(controller.signal)
      .then((data) => {
        if (!active) return;
        hasLoadedRef.current = true;
        setState({ data, error: null, isLoading: false, isRefreshing: false });
      })
      .catch((error) => {
        if (!active || error?.name === 'AbortError') return;
        setState((previous) => ({
          data: previous.data,
          error,
          isLoading: false,
          isRefreshing: false,
        }));
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [runFetch, enabled]);

  return state;
}
