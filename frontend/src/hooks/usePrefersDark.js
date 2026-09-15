import { useEffect, useState } from 'react';

const QUERY = '(prefers-color-scheme: dark)';

/**
 * Tracks the OS colour scheme so charts can use hues stepped for the dark
 * surface rather than the light ones dimmed — a flipped palette fails the
 * contrast and lightness bands.
 */
export function usePrefersDark() {
  const [isDark, setIsDark] = useState(
    () => window.matchMedia?.(QUERY).matches ?? false,
  );

  useEffect(() => {
    const media = window.matchMedia?.(QUERY);
    if (!media) return undefined;
    const onChange = (event) => setIsDark(event.matches);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);

  return isDark;
}
