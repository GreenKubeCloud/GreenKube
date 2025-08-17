# tests/core/test_scheduler.py

from unittest.mock import patch, MagicMock
from src.greenkube.core.scheduler import Scheduler

# A simple function to be used as a mock job
def dummy_job():
    pass

@patch('schedule.every')
def test_add_job_schedules_correctly(mock_every):
    """
    Tests that the Scheduler's add_job method correctly calls the 'schedule'
    library with the right parameters.
    """
    # Arrange
    # We create a mock object that allows us to chain calls like schedule.every(1).hours.do(...)
    mock_hours = MagicMock()
    mock_every.return_value.hours = mock_hours

    scheduler = Scheduler()
    interval = 5

    # Act
    scheduler.add_job(dummy_job, interval_hours=interval)

    # Assert
    # Check that schedule.every(5) was called
    mock_every.assert_called_once_with(interval)
    # Check that .hours.do(dummy_job) was called on the result
    mock_hours.do.assert_called_once_with(dummy_job)

