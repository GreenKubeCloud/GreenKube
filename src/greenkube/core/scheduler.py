import asyncio
import logging
import random
import re
import time
from typing import Callable, Coroutine, List

logger = logging.getLogger(__name__)

# Maximum consecutive failures before capping the backoff delay
_MAX_BACKOFF_MULTIPLIER = 8


class Scheduler:
    """
    Manages the scheduling and execution of periodic async tasks using asyncio.
    """

    def __init__(self):
        self.tasks: List[asyncio.Task] = []
        logger.info("AsyncScheduler initialized.")

    @staticmethod
    def seconds_until_boundary(interval_seconds: float, offset_seconds: float = 0.0, now: float | None = None) -> float:
        """Seconds until the next ``k × interval + offset`` instant (Unix time), always > 0."""
        current = time.time() if now is None else now
        elapsed = (current - offset_seconds) % interval_seconds
        return interval_seconds - elapsed if elapsed > 0 else interval_seconds

    async def _run_periodically(
        self,
        interval_seconds: int | float,
        job_func: Callable[[], Coroutine],
        skip_initial: bool = False,
        align_offset_seconds: float | None = None,
    ):
        """Internal loop to run a job periodically with jitter and exponential backoff.

        With ``align_offset_seconds`` the job runs at ``k × interval + offset``
        wall-clock instants instead (no jitter), so a collection job fires right
        after each window closes; a failed run simply retries at the next boundary.
        """
        if align_offset_seconds is not None:
            await self._run_aligned(interval_seconds, job_func, align_offset_seconds, skip_initial)
            return
        consecutive_failures = 0
        first_run = True
        loop = asyncio.get_running_loop()
        try:
            while True:
                if first_run and skip_initial:
                    first_run = False
                    await asyncio.sleep(interval_seconds)
                    continue
                first_run = False
                start = loop.time()
                try:
                    await job_func()
                    consecutive_failures = 0
                except Exception as e:
                    consecutive_failures += 1
                    logger.exception("Error in scheduled job '%s': %s", job_func.__name__, e)

                # Calculate how long to sleep.
                # On success: sleep until next_run = start + interval (± jitter).
                # On failure: wait at least interval × 2^failures (capped at
                # _MAX_BACKOFF_MULTIPLIER × interval) so a failing or overrunning
                # job cannot degenerate into a 1-second hot loop.
                elapsed = loop.time() - start
                sleep_time = max(interval_seconds - elapsed, 0)

                if consecutive_failures > 0:
                    backoff_multiplier = min(2**consecutive_failures, _MAX_BACKOFF_MULTIPLIER)
                    sleep_time = max(sleep_time, interval_seconds * backoff_multiplier)

                # Add ±10% jitter to spread load across replicas
                jitter = sleep_time * 0.1 * (2 * random.random() - 1)
                sleep_time = max(sleep_time + jitter, 1.0)

                await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            logger.info("Job '%s' cancelled.", job_func.__name__)
            raise

    async def _run_aligned(
        self,
        interval_seconds: int | float,
        job_func: Callable[[], Coroutine],
        offset_seconds: float,
        skip_initial: bool,
    ) -> None:
        try:
            if not skip_initial:
                await self._run_once(job_func)
            while True:
                await asyncio.sleep(self.seconds_until_boundary(interval_seconds, offset_seconds))
                await self._run_once(job_func)
        except asyncio.CancelledError:
            logger.info("Job '%s' cancelled.", job_func.__name__)
            raise

    @staticmethod
    async def _run_once(job_func: Callable[[], Coroutine]) -> None:
        try:
            await job_func()
        except Exception as e:
            logger.exception("Error in scheduled job '%s': %s", job_func.__name__, e)

    def add_job(
        self,
        job_func: Callable[[], Coroutine],
        interval_hours: int = 0,
        interval_minutes: int = 0,
        skip_initial: bool = False,
    ):
        """
        Adds a new async job to the schedule.

        Args:
            skip_initial: If True, the first execution is skipped (sleep first).
                          Use this when you manually call the function at startup.
        """
        interval_seconds = 0
        if interval_hours > 0:
            interval_seconds = interval_hours * 3600
            logger.info("Scheduled job '%s' to run every %s hour(s).", job_func.__name__, interval_hours)
        elif interval_minutes > 0:
            interval_seconds = interval_minutes * 60
            logger.info("Scheduled job '%s' to run every %s minute(s).", job_func.__name__, interval_minutes)

        if interval_seconds > 0:
            task = asyncio.create_task(self._run_periodically(interval_seconds, job_func, skip_initial=skip_initial))
            self.tasks.append(task)
        else:
            logger.warning(
                "Job '%s' was not scheduled: provide a positive interval_hours or interval_minutes.",
                job_func.__name__,
            )

    def add_job_from_string(
        self,
        job_func: Callable[[], Coroutine],
        interval_str: str,
        skip_initial: bool = False,
        align_offset_seconds: float | None = None,
    ):
        """
        Adds a job based on a Prometheus-style duration string like '5m' or '1h'.

        ``align_offset_seconds`` runs the job on wall-clock boundaries of the
        interval shifted by the offset (see :meth:`_run_periodically`).
        """
        match = re.match(r"^(\d+)([smh])$", interval_str.lower())
        if not match:
            raise ValueError(f"Invalid interval format: '{interval_str}'. Use 's', 'm', or 'h'.")

        value, unit = int(match.group(1)), match.group(2)
        if value <= 0:
            raise ValueError(f"Interval must be greater than zero: '{interval_str}'.")
        multipliers = {"s": 1, "m": 60, "h": 3600}
        interval_seconds = value * multipliers[unit]

        task = asyncio.create_task(
            self._run_periodically(
                interval_seconds,
                job_func,
                skip_initial=skip_initial,
                align_offset_seconds=align_offset_seconds,
            )
        )
        self.tasks.append(task)
        logger.info("Scheduled job '%s' to run every %s.", job_func.__name__, interval_str)

    async def stop(self):
        """Cancels all scheduled tasks."""
        logger.info("Stopping scheduler...")
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
