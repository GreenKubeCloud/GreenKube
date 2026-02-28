import asyncio
import logging
import random
import re
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

    async def _run_periodically(self, interval_seconds: int, job_func: Callable[[], Coroutine]):
        """Internal loop to run a job periodically with jitter and exponential backoff."""
        consecutive_failures = 0
        try:
            while True:
                start = asyncio.get_event_loop().time()
                try:
                    await job_func()
                    consecutive_failures = 0
                except Exception as e:
                    consecutive_failures += 1
                    logger.exception("Error in scheduled job '%s': %s", job_func.__name__, e)

                # Calculate how long to sleep.
                # On success: sleep until next_run = start + interval (± jitter).
                # On failure: apply exponential backoff capped at _MAX_BACKOFF_MULTIPLIER × interval.
                elapsed = asyncio.get_event_loop().time() - start
                base_sleep = max(interval_seconds - elapsed, 0)

                if consecutive_failures > 0:
                    backoff = min(2**consecutive_failures, _MAX_BACKOFF_MULTIPLIER)
                    base_sleep = min(base_sleep * backoff, interval_seconds * _MAX_BACKOFF_MULTIPLIER)

                # Add ±10% jitter to spread load across replicas
                jitter = base_sleep * 0.1 * (2 * random.random() - 1)
                sleep_time = max(base_sleep + jitter, 1.0)

                await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            logger.info("Job '%s' cancelled.", job_func.__name__)
            raise

    def add_job(self, job_func: Callable[[], Coroutine], interval_hours: int = 0, interval_minutes: int = 0):
        """
        Adds a new async job to the schedule.
        """
        interval_seconds = 0
        if interval_hours > 0:
            interval_seconds = interval_hours * 3600
            logger.info("Scheduled job '%s' to run every %s hour(s).", job_func.__name__, interval_hours)
        elif interval_minutes > 0:
            interval_seconds = interval_minutes * 60
            logger.info("Scheduled job '%s' to run every %s minute(s).", job_func.__name__, interval_minutes)

        if interval_seconds > 0:
            task = asyncio.create_task(self._run_periodically(interval_seconds, job_func))
            self.tasks.append(task)

    def add_job_from_string(self, job_func: Callable[[], Coroutine], interval_str: str):
        """
        Adds a job based on a Prometheus-style duration string like '5m' or '1h'.
        """
        match = re.match(r"^(\d+)([smh])$", interval_str.lower())
        if not match:
            raise ValueError(f"Invalid interval format: '{interval_str}'. Use 's', 'm', or 'h'.")

        value, unit = int(match.group(1)), match.group(2)
        multipliers = {"s": 1, "m": 60, "h": 3600}
        interval_seconds = value * multipliers[unit]

        task = asyncio.create_task(self._run_periodically(interval_seconds, job_func))
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
