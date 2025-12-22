# tests/core/test_scheduler.py

from unittest.mock import MagicMock

import pytest

from greenkube.core.scheduler import Scheduler


@pytest.mark.asyncio
async def test_add_job_schedules_correctly():
    """
    Tests that the Scheduler's add_job method correctly adds a task to the asyncio loop.
    """
    scheduler = Scheduler()
    mock_job = MagicMock()

    async def async_job():
        mock_job()

    # Act
    scheduler.add_job(async_job, interval_hours=1)

    # Assert
    assert len(scheduler.tasks) == 1
    task = scheduler.tasks[0]
    assert not task.done()

    await scheduler.stop()


@pytest.mark.asyncio
async def test_add_job_from_string_schedules_correctly():
    """
    Tests that add_job_from_string parses correct string and adds task.
    """
    scheduler = Scheduler()
    mock_job = MagicMock()

    async def async_job():
        mock_job()

    # Act
    scheduler.add_job_from_string(async_job, "1h")

    # Assert
    assert len(scheduler.tasks) == 1
    await scheduler.stop()
