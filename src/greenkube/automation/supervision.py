"""Supervision for in-process automation loops (roadmap D9, BUG-51).

An in-process loop (the inline plan worker, later the automation controller loop) must
never die silently while the API keeps reporting ready. ``SupervisedLoop`` restarts a
failed or exited loop with jittered exponential backoff, keeps a heartbeat the loop
refreshes on every iteration, and exposes a readiness verdict based on that heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 300.0
# A plan operation holds a 15-minute lease and heartbeats inside long provider calls,
# so only a loop silent for longer than that is considered wedged.
DEFAULT_LOOP_STALE_AFTER = timedelta(minutes=20)

Heartbeat = Callable[[], None]
LoopFactory = Callable[[Heartbeat], Awaitable[Any]]


def backoff_delay(
    failures: int,
    *,
    initial: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
    maximum: float = DEFAULT_MAX_BACKOFF_SECONDS,
    jitter: Callable[[float, float], float] = random.uniform,
) -> float:
    """Full-jitter exponential backoff: a random delay in ``[base/2, base]``."""
    if failures <= 0:
        return 0.0
    base = min(maximum, initial * (2 ** min(failures - 1, 32)))
    return max(0.0, jitter(base / 2, base))


def error_code(error: BaseException) -> str:
    """Stable, secret-free code for logs and status: the error's ``code`` or its type name."""
    code = getattr(error, "code", None)
    code = getattr(code, "value", code)
    if isinstance(code, str) and code and len(code) <= 64 and code.replace("_", "").replace("-", "").isalnum():
        return code
    return type(error).__name__


@dataclass
class LoopHealth:
    """Observable state of one supervised loop."""

    name: str
    stale_after: timedelta = DEFAULT_LOOP_STALE_AFTER
    state: str = "starting"
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    restarts: int = 0
    consecutive_failures: int = 0
    last_error_code: str | None = None
    last_error_at: datetime | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc), repr=False)

    def beat(self) -> None:
        self.heartbeat_at = self.clock()

    def heartbeat_age(self, now: datetime | None = None) -> float | None:
        if self.heartbeat_at is None:
            return None
        return max(0.0, ((now or self.clock()) - self.heartbeat_at).total_seconds())

    def ready(self, now: datetime | None = None) -> bool:
        """Ready while the loop runs (or backs off between restarts) with a fresh heartbeat."""
        if self.state not in {"running", "backoff"}:
            return False
        age = self.heartbeat_age(now)
        return age is not None and age <= self.stale_after.total_seconds()

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or self.clock()
        return {
            "name": self.name,
            "state": self.state,
            "ready": self.ready(current),
            "heartbeat_at": _iso(self.heartbeat_at),
            "heartbeat_age_seconds": self.heartbeat_age(current),
            "stale_after_seconds": self.stale_after.total_seconds(),
            "restarts": self.restarts,
            "last_error_code": self.last_error_code,
            "last_error_at": _iso(self.last_error_at),
        }


class SupervisedLoop:
    """Run a long-lived loop, restarting it with jittered backoff whenever it fails or exits.

    ``factory`` receives a heartbeat callable it should invoke on every iteration. A
    done-callback on the supervising task restarts it with backoff should the supervisor
    itself die unexpectedly; cancellation (shutdown) stops it for good.
    """

    def __init__(
        self,
        name: str,
        factory: LoopFactory,
        *,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        max_backoff: float = DEFAULT_MAX_BACKOFF_SECONDS,
        stale_after: timedelta = DEFAULT_LOOP_STALE_AFTER,
        jitter: Callable[[float, float], float] = random.uniform,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        if initial_backoff <= 0 or max_backoff < initial_backoff:
            raise ValueError("backoff bounds must be positive and ordered")
        self.name = name
        self._factory = factory
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._jitter = jitter
        self._sleep = sleep
        self._on_failure = on_failure
        self.health = LoopHealth(name=name, stale_after=stale_after, clock=clock)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping = False
        self._task = asyncio.create_task(self._supervise(), name=f"supervised-{self.name}")
        self._task.add_done_callback(self._on_supervisor_done)
        return self._task

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.health.state = "stopped"

    async def _supervise(self) -> None:
        while True:
            self.health.state = "running"
            self.health.started_at = self.health.clock()
            self.health.beat()
            try:
                await self._factory(self.health.beat)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = error_code(exc)
                logger.error(
                    "Supervised loop %s failed; restarting with backoff. error_code=%s error_type=%s",
                    self.name,
                    code,
                    type(exc).__name__,
                )
            else:
                code = "loop_exited"
                logger.warning("Supervised loop %s exited unexpectedly; restarting with backoff.", self.name)
            await self._record_failure_and_wait(code)

    async def _record_failure_and_wait(self, code: str) -> None:
        now = self.health.clock()
        started_at = self.health.started_at
        # A loop that ran for longer than the maximum backoff before failing starts over.
        if started_at is not None and (now - started_at).total_seconds() > self._max_backoff:
            self.health.consecutive_failures = 0
        self.health.consecutive_failures += 1
        self.health.restarts += 1
        self.health.last_error_code = code
        self.health.last_error_at = now
        self.health.state = "backoff"
        if self._on_failure is not None:
            try:
                self._on_failure(code)
            except Exception:
                logger.exception("Supervised loop %s failure hook raised.", self.name)
        delay = backoff_delay(
            self.health.consecutive_failures,
            initial=self._initial_backoff,
            maximum=self._max_backoff,
            jitter=self._jitter,
        )
        self.health.beat()
        await self._sleep(delay)

    def _on_supervisor_done(self, task: asyncio.Task[None]) -> None:
        if self._stopping or task.cancelled():
            self.health.state = "stopped"
            return
        error = task.exception()
        code = error_code(error) if error is not None else "supervisor_exited"
        logger.critical("Supervisor for loop %s died (error_code=%s); restarting it.", self.name, code)
        self.health.state = "failed"
        self.health.last_error_code = code
        self.health.last_error_at = self.health.clock()
        self.health.restarts += 1
        delay = backoff_delay(
            max(1, self.health.consecutive_failures),
            initial=self._initial_backoff,
            maximum=self._max_backoff,
            jitter=self._jitter,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_later(delay, self._restart_after_crash)

    def _restart_after_crash(self) -> None:
        if not self._stopping and (self._task is None or self._task.done()):
            self.start()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
