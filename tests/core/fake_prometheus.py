"""A tiny PromQL evaluator for range-collection tests.

It understands exactly the shapes produced by :class:`PromQLCatalog`:
``sum|max(rate(metric{matchers}[w])) by (labels)`` and
``sum|max(metric{matchers}) by (labels)``.  Raw series values are given as
already-computed per-second rates (for counters) or gauge values, so tests can
reason about exact numbers while still exercising the label matchers that
decide whether pod cgroup series are double counted.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Union

Value = Union[float, Callable[[float], float]]

_MATCHER_RE = re.compile(r"""([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*["']([^"']*)["']""")
_BY_RE = re.compile(r"by\s*\(([^)]*)\)")


@dataclass
class RawSeries:
    metric: str
    labels: dict[str, str]
    value: Value

    def at(self, ts: float) -> float:
        return self.value(ts) if callable(self.value) else float(self.value)


@dataclass
class FakePrometheus:
    series: list[RawSeries] = field(default_factory=list)
    queries: list[tuple[str, float, float, str | None]] = field(default_factory=list)
    fail_queries_containing: tuple[str, ...] = ()

    def add(self, metric: str, labels: dict[str, str], value: Value) -> None:
        self.series.append(RawSeries(metric, dict(labels), value))

    async def collect_range(self, start, end, step=None, query=None, **_kwargs):
        assert query is not None
        start_ts = start.timestamp() if isinstance(start, datetime) else float(start)
        end_ts = end.timestamp() if isinstance(end, datetime) else float(end)
        step_seconds = _parse_step(step)
        self.queries.append((query, start_ts, end_ts, step))
        for needle in self.fail_queries_containing:
            if needle in query:
                raise RuntimeError(f"prometheus unavailable for {needle}")

        metric_name, matchers = self._selector(query)
        by_match = _BY_RE.search(query)
        by = [label.strip() for label in by_match.group(1).split(",")] if by_match else []
        use_max = query.lstrip().startswith("max")

        timestamps = []
        t = start_ts
        while t <= end_ts + 1e-9:
            timestamps.append(t)
            t += step_seconds

        grouped: dict[tuple[str, ...], dict[float, float]] = defaultdict(dict)
        for raw in self.series:
            if raw.metric != metric_name or not _matches(raw.labels, matchers):
                continue
            key = tuple(raw.labels.get(label, "") for label in by)
            for ts in timestamps:
                value = raw.at(ts)
                if value is None:
                    continue
                current = grouped[key].get(ts)
                if current is None:
                    grouped[key][ts] = value
                elif use_max:
                    grouped[key][ts] = max(current, value)
                else:
                    grouped[key][ts] = current + value

        results = []
        for key, samples in grouped.items():
            labels = {label: value for label, value in zip(by, key) if value}
            results.append(
                {
                    "metric": labels,
                    "values": [[ts, _format(value)] for ts, value in sorted(samples.items())],
                }
            )
        return results

    async def collect_pod_storage(self):
        return {}

    async def close(self):
        return None

    def _selector(self, query: str) -> tuple[str, list[tuple[str, str, str]]]:
        names = {raw.metric for raw in self.series}
        candidates = re.findall(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^}]*\})?", query)
        for name, selector in candidates:
            if name in names or name.startswith(("container_", "kube_")):
                return name, _MATCHER_RE.findall(selector or "")
        raise AssertionError(f"Unsupported query: {query}")


def _matches(labels: dict[str, str], matchers: list[tuple[str, str, str]]) -> bool:
    for label, op, expected in matchers:
        actual = labels.get(label, "")
        if op == "=" and actual != expected:
            return False
        if op == "!=" and actual == expected:
            return False
        if op == "=~" and not re.fullmatch(expected, actual):
            return False
        if op == "!~" and re.fullmatch(expected, actual):
            return False
    return True


def _parse_step(step) -> float:
    if step is None:
        return 300.0
    if isinstance(step, (int, float)):
        return float(step)
    match = re.fullmatch(r"(\d+)([smh])", str(step))
    assert match, step
    return float(int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)])


def _format(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value))
