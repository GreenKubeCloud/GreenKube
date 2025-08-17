# src/greenkube/core/scheduler.py

import schedule
import time
from typing import Callable

class Scheduler:
    """
    Manages the scheduling and execution of periodic tasks, such as data collection.
    """

    def __init__(self):
        print("Scheduler initialized.")

    def add_job(self, job_func: Callable, interval_hours: int):
        """
        Adds a new job to the schedule.

        Args:
            job_func (Callable): The function to be executed.
            interval_hours (int): The interval in hours at which to run the job.
        """
        schedule.every(interval_hours).hours.do(job_func)
        print(f"Scheduled job '{job_func.__name__}' to run every {interval_hours} hour(s).")

    def run_pending(self):
        """
        Runs all jobs that are scheduled to run.
        This method should be called in a loop.
        """
        schedule.run_pending()

