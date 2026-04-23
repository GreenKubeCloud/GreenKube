# tests/core/test_scheduler_coverage.py
"""
Extended tests for Scheduler — backoff, failure handling, stop, and
add_job_from_string edge cases (54% → target ≥ 85%).
"""

import asyncio

import pytest

from greenkube.core.scheduler import Scheduler

# ---------------------------------------------------------------------------
# add_job_from_string — parsing
# ---------------------------------------------------------------------------


class TestAddJobFromString:
    """add_job_from_string parses Prometheus-style duration strings."""

    @pytest.mark.asyncio
    async def test_seconds_string_schedules_task(self):
        scheduler = Scheduler()

        async def noop():
            pass

        scheduler.add_job_from_string(noop, "30s")
        assert len(scheduler.tasks) == 1
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_minutes_string_schedules_task(self):
        scheduler = Scheduler()

        async def noop():
            pass

        scheduler.add_job_from_string(noop, "5m")
        assert len(scheduler.tasks) == 1
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_hours_string_schedules_task(self):
        scheduler = Scheduler()

        async def noop():
            pass

        scheduler.add_job_from_string(noop, "2h")
        assert len(scheduler.tasks) == 1
        await scheduler.stop()

    def test_invalid_format_raises_value_error(self):
        """Unrecognised interval strings must raise ValueError immediately."""
        scheduler = Scheduler()

        async def noop():
            pass

        with pytest.raises(ValueError, match="Invalid interval format"):
            scheduler.add_job_from_string(noop, "1d")  # Days not supported

    def test_empty_string_raises_value_error(self):
        scheduler = Scheduler()

        async def noop():
            pass

        with pytest.raises(ValueError):
            scheduler.add_job_from_string(noop, "")


# ---------------------------------------------------------------------------
# add_job — interval variants
# ---------------------------------------------------------------------------


class TestAddJob:
    """add_job accepts hours and minutes independently."""

    @pytest.mark.asyncio
    async def test_add_job_interval_minutes(self):
        scheduler = Scheduler()

        async def noop():
            pass

        scheduler.add_job(noop, interval_minutes=5)
        assert len(scheduler.tasks) == 1
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_add_job_zero_interval_does_not_schedule(self):
        """add_job with both interval_hours=0 and interval_minutes=0 adds no task."""
        scheduler = Scheduler()

        async def noop():
            pass

        scheduler.add_job(noop, interval_hours=0, interval_minutes=0)
        assert len(scheduler.tasks) == 0

    @pytest.mark.asyncio
    async def test_multiple_jobs_all_tracked(self):
        scheduler = Scheduler()

        async def job1():
            pass

        async def job2():
            pass

        scheduler.add_job(job1, interval_hours=1)
        scheduler.add_job(job2, interval_minutes=30)
        assert len(scheduler.tasks) == 2
        await scheduler.stop()


# ---------------------------------------------------------------------------
# stop — cancels all tasks cleanly
# ---------------------------------------------------------------------------


class TestSchedulerStop:
    @pytest.mark.asyncio
    async def test_stop_cancels_all_tasks(self):
        """After stop(), all tasks are cancelled and the list is cleared."""
        scheduler = Scheduler()

        async def noop():
            await asyncio.sleep(3600)

        scheduler.add_job(noop, interval_hours=1)
        assert len(scheduler.tasks) == 1

        await scheduler.stop()

        assert len(scheduler.tasks) == 0

    @pytest.mark.asyncio
    async def test_stop_on_empty_scheduler_is_safe(self):
        """Calling stop() with no tasks must not raise."""
        scheduler = Scheduler()
        await scheduler.stop()  # Should not raise
        assert len(scheduler.tasks) == 0


# ---------------------------------------------------------------------------
# _run_periodically — execution and failure handling
# ---------------------------------------------------------------------------


class TestRunPeriodically:
    """The internal job loop runs the function, resets failure count on success."""

    @pytest.mark.asyncio
    async def test_job_executed_on_first_run(self):
        """The job function is called at least once during the periodic loop."""
        executed = []

        async def job():
            executed.append(1)

        scheduler = Scheduler()
        task = asyncio.create_task(scheduler._run_periodically(0.05, job))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(executed) >= 1

    @pytest.mark.asyncio
    async def test_job_failure_does_not_stop_loop(self):
        """If the job raises, the task stays alive (is still cancellable), not dead."""
        calls = []

        async def flaky_job():
            calls.append(1)
            raise RuntimeError("always fails")

        scheduler = Scheduler()
        task = asyncio.create_task(scheduler._run_periodically(3600, flaky_job))
        # Give the event loop one tick to execute the first iteration
        await asyncio.sleep(0.01)
        # Task should still be running (not done) — the exception was caught internally
        assert not task.done(), "Task must not terminate on job failure"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(calls) >= 1

    @pytest.mark.asyncio
    async def test_skip_initial_defers_first_execution(self):
        """With skip_initial=True, the first tick is skipped (sleep before first run)."""
        calls = []

        async def job():
            calls.append(1)

        scheduler = Scheduler()
        task = asyncio.create_task(scheduler._run_periodically(10, job, skip_initial=True))
        await asyncio.sleep(0.05)  # Less than the 10-second interval
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Job should not have run at all within the short window
        assert len(calls) == 0
