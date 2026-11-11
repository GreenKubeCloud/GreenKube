"""Interval arithmetic shared by the controller loops.

The controller schedules every periodic job with
:class:`greenkube.core.loops.LoopRegistry` (leader gating, timeouts, per-loop
metrics, health and persisted heartbeats; roadmap M8). The former ``Scheduler``
class was removed once nothing used it; only these pure helpers remain.
"""

import re
import time

_INTERVAL_PATTERN = re.compile(r"^(\d+)([smh])$")
_INTERVAL_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600}


def parse_interval_seconds(interval_str: str) -> int:
    """Parse a Prometheus-style duration such as ``'30s'``, ``'5m'`` or ``'1h'`` into seconds."""
    match = _INTERVAL_PATTERN.match(str(interval_str).lower())
    if not match:
        raise ValueError(f"Invalid interval format: '{interval_str}'. Use 's', 'm', or 'h'.")
    value, unit = int(match.group(1)), match.group(2)
    if value <= 0:
        raise ValueError(f"Interval must be greater than zero: '{interval_str}'.")
    return value * _INTERVAL_MULTIPLIERS[unit]


def seconds_until_boundary(interval_seconds: float, offset_seconds: float = 0.0, now: float | None = None) -> float:
    """Seconds until the next ``k × interval + offset`` instant (Unix time), always > 0."""
    current = time.time() if now is None else now
    elapsed = (current - offset_seconds) % interval_seconds
    return interval_seconds - elapsed if elapsed > 0 else interval_seconds


__all__ = ["parse_interval_seconds", "seconds_until_boundary"]
