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
from typing import Callable, Optional, Union

Value = Union[float, Callable[[float], Optional[float]]]

_MATCHER_RE = re.compile(r"""([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*["']([^"']*)["']""")
_BY_RE = re.compile(r"by\s*\(([^)]*)\)")


@dataclass
class RawSeries:
    metric: str
    labels: dict[str, str]
    value: Value

    def at(self, ts: float) -> Optional[float]:
        return self.value(ts) if callable(self.value) else float(self.value)


@dataclass
class FakePrometheus:
    series: list[RawSeries] = field(default_factory=list)
    queries: list[tuple[str, float, float, str | None]] = field(default_factory=list)
    fail_queries_containing: tuple[str, ...] = ()
    #: When set, ``avg_over_time``/``max_over_time`` of a ``[Ns]`` range read the raw
    #: values at this scrape interval over ``(t - N, t]`` instead of the value at ``t``
    #: (COL-12, COL-21).  Off by default: older tests give instant values.
    window_scrape_seconds: float | None = None

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

        timestamps = []
        t = start_ts
        while t <= end_ts + 1e-9:
            timestamps.append(t)
            t += step_seconds
        if _is_composite(query):
            return [
                {"metric": labels, "values": [[ts, _format(value)] for ts, value in sorted(samples.items())]}
                for labels, samples in self._evaluate(query, timestamps).values()
            ]

        metric_name, matchers = self._selector(query)
        by_match = _BY_RE.search(query)
        by = [label.strip() for label in by_match.group(1).split(",")] if by_match else []
        use_max = query.lstrip().startswith("max")
        # ``avg_over_time(metric[w])`` keeps every label of every series (DCGM GPU queries).
        keep_labels = query.lstrip().startswith("avg_over_time(")

        grouped: dict[tuple, dict[float, float]] = defaultdict(dict)
        group_labels: dict[tuple, dict[str, str]] = {}
        over_time = _OVER_TIME_RE.search(query) if self.window_scrape_seconds else None
        for raw in self.series:
            if raw.metric != metric_name or not _matches(raw.labels, matchers):
                continue
            if over_time is not None:
                raw = self._windowed(raw, over_time.group(1), float(over_time.group(2)))
            if keep_labels:
                key: tuple = tuple(sorted(raw.labels.items()))
                group_labels[key] = dict(raw.labels)
            else:
                by_values: tuple[str, ...] = tuple(raw.labels.get(label, "") for label in by)
                key = by_values
                group_labels[key] = {label: value for label, value in zip(by, by_values) if value}
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
            labels = group_labels[key]
            results.append(
                {
                    "metric": labels,
                    "values": [[ts, _format(value)] for ts, value in sorted(samples.items())],
                }
            )
        return results

    def _windowed(self, raw: RawSeries, function: str, window: float) -> RawSeries:
        """``function_over_time(raw[window])`` sampled every ``window_scrape_seconds``."""
        scrape = float(self.window_scrape_seconds or window)
        points = max(int(round(window / scrape)), 1)

        def value(ts: float):
            samples = [raw.at(ts - window + scrape * (k + 1)) for k in range(points)]
            samples = [sample for sample in samples if sample is not None]
            if not samples:
                return None
            return max(samples) if function == "max" else sum(samples) / len(samples)

        return RawSeries(raw.metric, raw.labels, value)

    def _evaluate(self, expr: str, timestamps: list[float]) -> dict[tuple, tuple[dict[str, str], dict[float, float]]]:
        """Evaluate ``a or b`` and nested ``sum|max(inner) by (labels)`` (COL-11, COL-21).

        Values are keyed by their label set; ``or`` keeps the left operand's
        series and adds right-hand series whose label set the left lacks.
        """
        operands = _split_or(expr)
        if len(operands) > 1:
            merged: dict[tuple, tuple[dict[str, str], dict[float, float]]] = {}
            for operand in operands:
                for key, item in self._evaluate(operand, timestamps).items():
                    merged.setdefault(key, item)
            return merged
        match = _AGG_RE.fullmatch(expr.strip())
        if match is None:
            metric_name, matchers = self._selector(expr)
            series: dict[tuple, tuple[dict[str, str], dict[float, float]]] = {}
            for raw in self.series:
                if raw.metric != metric_name or not _matches(raw.labels, matchers):
                    continue
                labels = {k: v for k, v in raw.labels.items() if v}
                values = {ts: value for ts in timestamps if (value := raw.at(ts)) is not None}
                series[tuple(sorted(labels.items()))] = (labels, values)
            return series
        op, inner, by_clause = match.groups()
        by = [label.strip() for label in by_clause.split(",") if label.strip()]
        groups: dict[tuple, tuple[dict[str, str], dict[float, float]]] = {}
        for labels, values in self._evaluate(inner, timestamps).values():
            grouped_labels = {label: labels[label] for label in by if labels.get(label)}
            key = tuple(sorted(grouped_labels.items()))
            _, current = groups.setdefault(key, (grouped_labels, {}))
            for ts, value in values.items():
                if ts not in current:
                    current[ts] = value
                else:
                    current[ts] = max(current[ts], value) if op == "max" else current[ts] + value
        return groups

    async def close(self):
        return None

    def _selector(self, query: str) -> tuple[str, list[tuple[str, str, str]]]:
        names = {raw.metric for raw in self.series}
        candidates = re.findall(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^}]*\})?", query)
        for name, selector in candidates:
            if name in names or name.startswith(("container_", "kube_")):
                return name, _MATCHER_RE.findall(selector or "")
        raise AssertionError(f"Unsupported query: {query}")


_OVER_TIME_RE = re.compile(r"(avg|max)_over_time\([^\[]*\[(\d+)s\]\)")
_AGG_RE = re.compile(r"(sum|max)\((.*)\)\s*by\s*\(([^)]*)\)", re.S)


def _split_or(expr: str) -> list[str]:
    """Split ``expr`` on top-level ``or`` operators (outside parentheses and braces)."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(expr):
        char = expr[i]
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
        elif depth == 0 and expr.startswith(" or ", i):
            parts.append(expr[start:i])
            start = i + 4
            i += 3
        i += 1
    parts.append(expr[start:])
    return [part.strip() for part in parts]


def _is_composite(query: str) -> bool:
    """Queries the legacy one-level evaluator cannot handle: ``or`` or nested ``by`` clauses."""
    return len(_split_or(query)) > 1 or len(_BY_RE.findall(query)) > 1


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
