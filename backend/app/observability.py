"""Small runtime-introspection helpers.

Kept separate from the pipeline so both the demo endpoint and the startup
bootstrap can report the same numbers.
"""

from __future__ import annotations

import resource
import sys


def peak_rss_mb() -> float:
    """Peak resident set size for this process, in MB.

    `ru_maxrss` is in bytes on macOS and kilobytes on Linux, which is a
    classic source of readings that are wrong by 1024x. Render runs Linux;
    development happens on macOS, so both are handled explicitly.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return raw / (1024 * 1024)
    return raw / 1024
