import asyncio
import logging
import re
from typing import Callable, Coroutine, List

logger = logging.getLogger(__name__)


class Scheduler:
    """
    Manages the scheduling and execution of periodic async tasks using asyncio.
    """

    def __init__(self):
        self.tasks: List[asyncio.Task] = []
        logger.info("AsyncScheduler initialized.")

    async def _run_periodically(self, interval_seconds: int, job_func: Callable[[], Coroutine]):
        """Internal loop to run a job periodically."""
        try:
            while True:
                try:
                    await job_func()
                except Exception as e:
                    logger.error(f"Error in scheduled job '{job_func.__name__}': {e}", exc_info=True)

                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info(f"Job '{job_func.__name__}' cancelled.")
            raise

    def add_job(self, job_func: Callable[[], Coroutine], interval_hours: int = 0, interval_minutes: int = 0):
        """
        Adds a new async job to the schedule.
        """
        interval_seconds = 0
        if interval_hours > 0:
            interval_seconds = interval_hours * 3600
            logger.info(f"Scheduled job '{job_func.__name__}' to run every {interval_hours} hour(s).")
        elif interval_minutes > 0:
            interval_seconds = interval_minutes * 60
            logger.info(f"Scheduled job '{job_func.__name__}' to run every {interval_minutes} minute(s).")

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
        logger.info(f"Scheduled job '{job_func.__name__}' to run every {interval_str}.")

    async def stop(self):
        """Cancels all scheduled tasks."""
        logger.info("Stopping scheduler...")
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
