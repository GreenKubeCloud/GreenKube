# tests/test_report_flags.py
"""
Tests for the `greenkube report` CLI flags and delegation to report_range.
"""

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from greenkube.cli import app

runner = CliRunner()


@pytest.mark.parametrize(
    "args",
    [
        ["report", "--today"],
        ["report", "--days", "1"],
        ["report", "--hours", "2"],
        ["report", "--minutes", "30"],
        ["report", "--weeks", "1"],
        ["report", "--monthly"],
        ["report", "--yearly"],
        ["report", "--output", "out.csv"],
    ],
)
def test_report_delegates_to_report_range(mocker, args):
    """When any range or output flag is present, `report` should delegate to `report_range`."""
    mocked_rr = mocker.patch("greenkube.cli.report_range")
    mocked_rr.return_value = None

    result = runner.invoke(app, args)
    assert result.exit_code == 0
    mocked_rr.assert_called_once()


def test_report_format_without_output_uses_processor(mocker):
    """`--format` by itself must not trigger range delegation; it should run the processor flow."""
    mock_proc = mocker.patch("greenkube.cli.get_processor")
    # Return a processor-like object whose run returns empty list
    proc_inst = MagicMock()
    proc_inst.run.return_value = []
    mock_proc.return_value = proc_inst

    # Patch ConsoleReporter to observe calls
    mock_reporter_class = mocker.patch("greenkube.cli.ConsoleReporter")
    mock_reporter_inst = MagicMock()
    mock_reporter_class.return_value = mock_reporter_inst

    result = runner.invoke(app, ["report", "--format", "json"])
    assert result.exit_code == 0
    # Since processor returned empty, reporter should not be called
    mock_reporter_inst.report.assert_not_called()
