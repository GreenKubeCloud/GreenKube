# src/greenkube/core/scheduler.py

import logging
import re
from typing import Callable

import schedule

logger = logging.getLogger(__name__)


class Scheduler:
    """
    Manages the scheduling and execution of periodic tasks, such as data collection.
    """

    def __init__(self):
        logger.info("Scheduler initialized.")

    def add_job(self, job_func: Callable, interval_hours: int = 0, interval_minutes: int = 0):
        """
        Adds a new job to the schedule.

        Args:
            job_func (Callable): The function to be executed.
            interval_hours (int): The interval in hours at which to run the job.
            interval_minutes (int): The interval in minutes at which to run the job.
        """
        if interval_hours > 0:
            schedule.every(interval_hours).hours.do(job_func)
            logger.info(f"Scheduled job '{job_func.__name__}' to run every {interval_hours} hour(s).")
        elif interval_minutes > 0:
            schedule.every(interval_minutes).minutes.do(job_func)
            logger.info(f"Scheduled job '{job_func.__name__}' to run every {interval_minutes} minute(s).")

    def add_job_from_string(self, job_func: Callable, interval_str: str):
        """Adds a job based on a Prometheus-style duration string like '5m' or '1h'."""
        match = re.match(r"^(\d+)([smh])$", interval_str.lower())
        if not match:
            raise ValueError(f"Invalid interval format: '{interval_str}'. Use 's', 'm', or 'h'.")

        value, unit = int(match.group(1)), match.group(2)
        job = schedule.every(value)
        getattr(job, {"s": "seconds", "m": "minutes", "h": "hours"}[unit]).do(job_func)
        logger.info(f"Scheduled job '{job_func.__name__}' to run every {interval_str}.")

    def run_pending(self):
        """
        Runs all jobs that are scheduled to run.
        This method should be called in a loop.
        """
        schedule.run_pending()
