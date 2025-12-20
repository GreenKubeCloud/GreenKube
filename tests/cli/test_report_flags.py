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
    mock_repo = mocker.MagicMock()
    mock_repo.read_combined_metrics = mocker.AsyncMock(return_value=[])
    mocker.patch("greenkube.cli.report.get_repository", return_value=mock_repo)

    result = runner.invoke(app, args)
    assert result.exit_code == 0
    mock_repo.read_combined_metrics.assert_called_once()


def test_report_format_without_output_uses_processor(mocker):
    """`--output` by itself must not trigger range delegation; it should run the processor flow."""
    # Patch the get_repository used by the report submodule
    repo_inst = MagicMock()
    repo_inst.read_combined_metrics = mocker.AsyncMock(return_value=[])
    mocker.patch("greenkube.cli.report.get_repository", return_value=repo_inst)

    result = runner.invoke(app, ["report", "--output", "json"])
    assert result.exit_code == 0
