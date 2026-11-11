"""Registry of the controller's periodic loops (roadmap M8 process topology, F8).

Every periodic job of the controller process (collection, scans, lifecycle and
measurement, attribution, GitOps discovery and sync, the automation controller
loop, plan execution, maintenance) is declared once as a :class:`LoopSpec` and run
by one :class:`LoopRegistry`:

- **Leader gating.** A ``leader_only`` loop executes only while this process holds
  the controller Lease (or when leader election is disabled). Each executed tick
  runs inside a :class:`~greenkube.core.leader_election.LeaderFence` scope so the
  job can re-check the Lease before it commits (A14).
- **Isolation.** Every loop has its own task. An exception or a timeout fails only
  that tick of that loop; the other loops keep running. A loop task that dies
  unexpectedly is restarted.
- **Cadence.** Fixed rate with jitter, or aligned on wall-clock boundaries;
  exponential backoff after failed ticks (capped per loop).
- **Telemetry.** ``greenkube_loop_runs_total{loop,outcome}``,
  ``greenkube_loop_duration_seconds{loop}``,
  ``greenkube_loop_last_success_timestamp_seconds{loop}``,
  ``greenkube_loop_consecutive_failures{loop}`` and ``greenkube_controller_leader``,
  plus a JSON snapshot and a readiness verdict served by the controller endpoint.

The registry owns *scheduling only*: the business logic of each loop stays in its
own module, and jobs that log their own errors can still mark the tick failed with
:func:`report_loop_failure` without raising (so their cadence is unchanged).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from greenkube.core.leader_election import LeaderGate, leader_fence_scope
from greenkube.core.observability import record_loop_run, set_controller_leader
from greenkube.core.scheduler import Scheduler

logger = logging.getLogger(__name__)

LOOP_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
MAX_LOOPS = 32
DEFAULT_MAX_BACKOFF_MULTIPLIER = 8
#: A loop task that died unexpectedly is restarted after this delay.
LOOP_RESTART_DELAY_SECONDS = 5.0
#: Grace added to the computed staleness threshold of every loop.
STALE_GRACE_SECONDS = 60.0
#: Floor between two ticks of the same loop (sub-second loops only in tests).
MIN_SLEEP_SECONDS = 1.0

LoopFunc = Callable[[], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class LoopSpec:
    """Declaration of one periodic loop.

    ``run_immediately`` runs the first tick at start instead of after one interval.
    ``align_offset_seconds`` runs ticks at ``k × interval + offset`` wall-clock
    instants (no jitter, no backoff: a failed tick retries at the next boundary).
    ``standby_recheck_seconds`` makes a leader-only loop re-check the Lease sooner
    than its interval while this process is a standby, so it starts right after a
    takeover. ``max_backoff_multiplier=1`` disables the failure backoff.
    """

    name: str
    func: LoopFunc
    interval_seconds: float
    leader_only: bool = True
    jitter: float = 0.1
    timeout_seconds: float | None = None
    run_immediately: bool = False
    align_offset_seconds: float | None = None
    max_backoff_multiplier: int = DEFAULT_MAX_BACKOFF_MULTIPLIER
    standby_recheck_seconds: float | None = None
    stale_after_seconds: float | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not LOOP_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("loop name must match ^[a-z][a-z0-9-]{0,47}$")
        if not callable(self.func):
            raise ValueError(f"loop {self.name!r} needs a callable job")
        _require_positive(self.name, "interval_seconds", self.interval_seconds)
        if isinstance(self.jitter, bool) or not isinstance(self.jitter, (int, float)) or not 0 <= self.jitter < 1:
            raise ValueError(f"loop {self.name!r}: jitter must be in [0, 1)")
        for attribute in ("timeout_seconds", "standby_recheck_seconds", "stale_after_seconds"):
            value = getattr(self, attribute)
            if value is not None:
                _require_positive(self.name, attribute, value)
        if self.align_offset_seconds is not None and (
            isinstance(self.align_offset_seconds, bool)
            or not isinstance(self.align_offset_seconds, (int, float))
            or not math.isfinite(self.align_offset_seconds)
            or self.align_offset_seconds < 0
        ):
            raise ValueError(f"loop {self.name!r}: align_offset_seconds must be a non-negative number")
        if type(self.max_backoff_multiplier) is not int or self.max_backoff_multiplier < 1:
            raise ValueError(f"loop {self.name!r}: max_backoff_multiplier must be an integer >= 1")

    @property
    def stale_after(self) -> float:
        """Seconds without a heartbeat after which the loop is considered wedged."""
        if self.stale_after_seconds is not None:
            return float(self.stale_after_seconds)
        longest_sleep = self.interval_seconds * self.max_backoff_multiplier * (1 + self.jitter)
        return longest_sleep + (self.timeout_seconds or 0.0) + STALE_GRACE_SECONDS


@dataclass
class LoopStatus:
    """Observable state of one registered loop."""

    name: str
    leader_only: bool
    interval_seconds: float
    timeout_seconds: float | None
    stale_after_seconds: float
    state: str = "registered"
    heartbeat_at: datetime | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_duration_seconds: float | None = None
    last_outcome: str | None = None
    last_error_code: str | None = None
    consecutive_failures: int = 0
    runs: int = 0
    failures: int = 0
    timeouts: int = 0
    skipped: int = 0
    restarts: int = 0

    def heartbeat_age(self, now: datetime) -> float | None:
        if self.heartbeat_at is None:
            return None
        return max(0.0, (now - self.heartbeat_at).total_seconds())

    def ready(self, now: datetime) -> bool:
        """A loop is healthy while its task lives and its heartbeat is fresh."""
        if self.state in {"crashed", "registered"}:
            return False
        if self.state == "stopped":
            return True
        age = self.heartbeat_age(now)
        if age is None:
            return False
        if self.state == "running" and self.timeout_seconds is None:
            # Unbounded jobs (plan execution, scans) heartbeat through their own leases.
            return age <= max(self.stale_after_seconds, 20 * 60.0)
        return age <= self.stale_after_seconds

    def snapshot(self, now: datetime) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "ready": self.ready(now),
            "leader_only": self.leader_only,
            "interval_seconds": self.interval_seconds,
            "timeout_seconds": self.timeout_seconds,
            "heartbeat_at": _iso(self.heartbeat_at),
            "heartbeat_age_seconds": self.heartbeat_age(now),
            "stale_after_seconds": self.stale_after_seconds,
            "last_started_at": _iso(self.last_started_at),
            "last_success_at": _iso(self.last_success_at),
            "last_failure_at": _iso(self.last_failure_at),
            "last_duration_seconds": self.last_duration_seconds,
            "last_outcome": self.last_outcome,
            "last_error_code": self.last_error_code,
            "consecutive_failures": self.consecutive_failures,
            "runs": self.runs,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "skipped": self.skipped,
            "restarts": self.restarts,
        }


@dataclass
class _TickReport:
    failed: bool = False
    error_code: str | None = None


_current_tick: ContextVar[_TickReport | None] = ContextVar("greenkube_loop_tick", default=None)


def report_loop_failure(error: BaseException | str | None = None) -> None:
    """Mark the current loop tick failed without raising.

    For jobs that log and swallow their own errors (so their cadence and the
    controller's initial work are unchanged) but must still show up in
    ``greenkube_loop_runs_total{outcome="failure"}``. A no-op outside a loop tick.
    """
    report = _current_tick.get()
    if report is None:
        return
    report.failed = True
    if report.error_code is None:
        report.error_code = error if isinstance(error, str) else (error_code(error) if error else "reported_failure")


def error_code(error: BaseException) -> str:
    """Stable, secret-free code for logs and status: the error's ``code`` or its type name."""
    code = getattr(error, "code", None)
    code = getattr(code, "value", code)
    if isinstance(code, str) and code and len(code) <= 64 and code.replace("_", "").replace("-", "").isalnum():
        return code
    return type(error).__name__


class LoopRegistry:
    """Run registered loops, each in its own task, with leader gating and telemetry."""

    def __init__(
        self,
        *,
        role: str = "controller",
        leader: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        wall_time: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.role = role
        self._leader = leader
        self._clock = clock
        self._wall_time = wall_time
        self._sleep = sleep
        self._rng = rng
        self._specs: dict[str, LoopSpec] = {}
        self._status: dict[str, LoopStatus] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self._started = False

    # -- registration ---------------------------------------------------------------

    def register(self, spec: LoopSpec) -> LoopSpec:
        if not isinstance(spec, LoopSpec):
            raise TypeError("register() takes a LoopSpec")
        if spec.name in self._specs:
            raise ValueError(f"loop {spec.name!r} is already registered")
        if len(self._specs) >= MAX_LOOPS:
            raise ValueError(f"at most {MAX_LOOPS} loops can be registered")
        self._specs[spec.name] = spec
        self._status[spec.name] = LoopStatus(
            name=spec.name,
            leader_only=spec.leader_only,
            interval_seconds=float(spec.interval_seconds),
            timeout_seconds=spec.timeout_seconds,
            stale_after_seconds=spec.stale_after,
        )
        if self._started and not self._stopping:
            self._start_loop(spec)
        return spec

    def set_leader(self, leader: Callable[[], bool] | None) -> None:
        """Install the Lease predicate (``None``: no leader election, this process leads)."""
        self._leader = leader

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def spec(self, name: str) -> LoopSpec:
        return self._specs[name]

    def status(self, name: str) -> LoopStatus:
        return self._status[name]

    def is_leader(self) -> bool:
        if self._leader is None:
            return True
        try:
            return bool(self._leader())
        except Exception:
            logger.warning("Leader predicate failed; leader-only loops are paused.")
            return False

    # -- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        """Start one task per registered loop (idempotent)."""
        self._stopping = False
        self._started = True
        set_controller_leader(self.is_leader())
        for spec in self._specs.values():
            task = self._tasks.get(spec.name)
            if task is None or task.done():
                self._start_loop(spec)

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for status in self._status.values():
            status.state = "stopped"
        self._started = False

    async def run_once(self, name: str) -> str:
        """Run one tick of ``name`` now (respecting leader gating); return its outcome."""
        outcome, _ = await self._tick(self._specs[name])
        return outcome

    # -- health -----------------------------------------------------------------------

    def ready(self, now: datetime | None = None) -> bool:
        current = now or self._clock()
        return all(status.ready(current) for status in self._status.values())

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or self._clock()
        loops = [status.snapshot(current) for status in self._status.values()]
        return {
            "role": self.role,
            "leader": self.is_leader(),
            "ready": all(loop["ready"] for loop in loops),
            "loops": loops,
        }

    # -- internals ----------------------------------------------------------------

    def _start_loop(self, spec: LoopSpec) -> None:
        status = self._status[spec.name]
        status.state = "idle"
        status.heartbeat_at = self._clock()
        task = asyncio.create_task(self._run(spec), name=f"loop-{spec.name}")
        task.add_done_callback(lambda finished, loop_spec=spec: self._on_loop_done(loop_spec, finished))
        self._tasks[spec.name] = task

    async def _run(self, spec: LoopSpec) -> None:
        if spec.align_offset_seconds is not None:
            if spec.run_immediately:
                await self._tick(spec)
            while True:
                await self._sleep(
                    Scheduler.seconds_until_boundary(
                        spec.interval_seconds, spec.align_offset_seconds, now=self._wall_time()
                    )
                )
                await self._tick(spec)
        if not spec.run_immediately:
            await self._sleep(spec.interval_seconds)
        while True:
            outcome, duration = await self._tick(spec)
            await self._sleep(self._next_delay(spec, outcome, duration))

    def _next_delay(self, spec: LoopSpec, outcome: str, duration: float) -> float:
        status = self._status[spec.name]
        interval = float(spec.interval_seconds)
        if outcome == "skipped" and spec.standby_recheck_seconds is not None:
            return min(interval, float(spec.standby_recheck_seconds))
        delay = max(interval - duration, 0.0)
        if outcome in {"failure", "timeout"} and status.consecutive_failures > 0:
            multiplier = min(2**status.consecutive_failures, spec.max_backoff_multiplier)
            delay = max(delay, interval * multiplier)
        if spec.jitter:
            delay += delay * spec.jitter * (2 * self._rng() - 1)
        return max(delay, min(MIN_SLEEP_SECONDS, interval))

    async def _tick(self, spec: LoopSpec) -> tuple[str, float]:
        status = self._status[spec.name]
        now = self._clock()
        status.heartbeat_at = now
        leader = self.is_leader()
        set_controller_leader(leader)
        if spec.leader_only and not leader:
            status.state = "standby"
            status.skipped += 1
            status.last_outcome = "skipped"
            record_loop_run(spec.name, "skipped", consecutive_failures=status.consecutive_failures)
            return "skipped", 0.0

        status.state = "running"
        status.last_started_at = now
        status.runs += 1
        report = _TickReport()
        token = _current_tick.set(report)
        started = time.monotonic()
        outcome = "success"
        code: str | None = None
        try:
            fence = self._leader.fence() if spec.leader_only and isinstance(self._leader, LeaderGate) else None
            with leader_fence_scope(fence):
                if spec.timeout_seconds is not None:
                    await asyncio.wait_for(spec.func(), timeout=spec.timeout_seconds)
                else:
                    await spec.func()
        except asyncio.CancelledError:
            status.state = "stopped"
            raise
        except TimeoutError:
            outcome, code = "timeout", "timeout"
            logger.error("Controller loop %s timed out after %.0fs.", spec.name, spec.timeout_seconds or 0)
        except Exception as exc:
            outcome, code = "failure", error_code(exc)
            logger.exception("Controller loop %s failed (error_code=%s).", spec.name, code)
        else:
            if report.failed:
                outcome, code = "failure", report.error_code
        finally:
            _current_tick.reset(token)

        duration = time.monotonic() - started
        finished = self._clock()
        status.heartbeat_at = finished
        status.last_finished_at = finished
        status.last_duration_seconds = duration
        status.last_outcome = outcome
        success_timestamp = None
        if outcome == "success":
            status.consecutive_failures = 0
            status.last_success_at = finished
            status.last_error_code = None
            success_timestamp = finished.timestamp()
            status.state = "idle"
        else:
            status.consecutive_failures += 1
            status.failures += 1
            status.timeouts += 1 if outcome == "timeout" else 0
            status.last_failure_at = finished
            status.last_error_code = code
            status.state = "backoff"
        record_loop_run(
            spec.name,
            outcome,
            duration_seconds=duration,
            consecutive_failures=status.consecutive_failures,
            success_timestamp=success_timestamp,
        )
        return outcome, duration

    def _on_loop_done(self, spec: LoopSpec, task: asyncio.Task[None]) -> None:
        if self._stopping or task.cancelled():
            return
        status = self._status[spec.name]
        error = task.exception()
        status.state = "crashed"
        status.restarts += 1
        status.last_error_code = error_code(error) if error is not None else "loop_exited"
        logger.critical(
            "Controller loop %s stopped unexpectedly (error_code=%s); restarting it.",
            spec.name,
            status.last_error_code,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_later(LOOP_RESTART_DELAY_SECONDS, self._restart, spec)

    def _restart(self, spec: LoopSpec) -> None:
        task = self._tasks.get(spec.name)
        if self._stopping or spec.name not in self._specs or (task is not None and not task.done()):
            return
        self._start_loop(spec)


def _require_positive(name: str, attribute: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"loop {name!r}: {attribute} must be a positive number")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "LoopRegistry",
    "LoopSpec",
    "LoopStatus",
    "error_code",
    "report_loop_failure",
]
