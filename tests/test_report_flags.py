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
        ["report", "--last", "1d"],
        ["report", "--last", "2h"],
        ["report", "--last", "30m"],
        ["report", "--last", "1w"],
        ["report", "--monthly"],
        ["report", "--yearly"],
    ],
)
def test_report_delegates_to_report_range(mocker, args):
    """When a range or grouping flag is present, `report` should call processor.run_range."""
    # Patch the get_processor used by the report submodule so run_range can be observed
    mock_proc = mocker.MagicMock()
    mock_proc.run_range = mocker.MagicMock(return_value=None)
    mocker.patch("greenkube.cli.report.get_processor", return_value=mock_proc)

    result = runner.invoke(app, args)
    assert result.exit_code == 0
    mock_proc.run_range.assert_called_once()


def test_report_format_without_output_uses_processor(mocker):
    """`--output` by itself must not trigger range delegation; it should run the processor flow."""
    # Patch the get_processor used by the report submodule
    proc_inst = MagicMock()
    proc_inst.run.return_value = []
    mocker.patch("greenkube.cli.report.get_processor", return_value=proc_inst)

    # Patch ConsoleReporter to observe calls
    mock_reporter_class = mocker.patch("greenkube.cli.ConsoleReporter")
    mock_reporter_inst = MagicMock()
    mock_reporter_class.return_value = mock_reporter_inst

    result = runner.invoke(app, ["report", "--output", "json"])
    assert result.exit_code == 0
    # Since processor returned empty, reporter should not be called
    mock_reporter_inst.report.assert_not_called()
