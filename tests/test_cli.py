# tests/test_cli.py
"""
Unit tests for the GreenKube Command-Line Interface (CLI).
"""

from unittest.mock import ANY, MagicMock

import pytest
from typer.testing import CliRunner

from greenkube.cli import app
from greenkube.models.metrics import CombinedMetric

runner = CliRunner()


@pytest.fixture
def mock_reporter(mocker):
    """
    Fixture to patch ConsoleReporter and provide a mock instance.
    """
    mock_reporter_class = mocker.patch("greenkube.cli.ConsoleReporter")
    mock_reporter_instance = MagicMock()
    mock_reporter_class.return_value = mock_reporter_instance
    return mock_reporter_instance


@pytest.fixture
def sample_combined_metrics():
    """
    Fixture to provide sample CombinedMetric data.
    """
    return [
        CombinedMetric(
            pod_name="pod-x",
            namespace="default",
            total_cost=1.0,
            co2e_grams=100.0,
            pue=1.5,
            grid_intensity=200.0,
        ),
        CombinedMetric(
            pod_name="pod-y",
            namespace="monitoring",
            total_cost=2.0,
            co2e_grams=200.0,
            pue=1.6,
            grid_intensity=210.0,
        ),
    ]


def test_report_success(mocker, mock_reporter, sample_combined_metrics):
    """
    Tests that the `greenkube report` command correctly calls the reporter with data.
    """
    # Patch the processor used by the report submodule
    mock_proc = mocker.patch("greenkube.cli.report.get_processor")
    proc_inst = MagicMock()
    proc_inst.run.return_value = sample_combined_metrics
    mock_proc.return_value = proc_inst

    result = runner.invoke(app, ["report"])

    assert result.exit_code == 0, "CLI should exit without errors"
    mock_reporter.report.assert_called_once_with(data=ANY)
    reported_data = mock_reporter.report.call_args.kwargs["data"]
    assert isinstance(reported_data, list)
    assert all(isinstance(item, CombinedMetric) for item in reported_data)
    assert len(reported_data) == 2


def test_report_with_namespace_filter_success(mocker, mock_reporter, sample_combined_metrics):
    """
    Tests that the CLI correctly filters data before passing it to the reporter.
    """
    mock_proc = mocker.patch("greenkube.cli.report.get_processor")
    proc_inst = MagicMock()
    proc_inst.run.return_value = sample_combined_metrics
    mock_proc.return_value = proc_inst

    result = runner.invoke(app, ["report", "--namespace", "monitoring"])

    assert result.exit_code == 0
    mock_reporter.report.assert_called_once_with(data=ANY)
    reported_data = mock_reporter.report.call_args.kwargs["data"]
    # Only the metric with namespace "monitoring" should be present
    assert len(reported_data) == 1
    assert reported_data[0].namespace == "monitoring"


def test_report_namespace_not_found(mocker, mock_reporter, sample_combined_metrics):
    """
    Tests that the CLI exits gracefully with a warning when a non-existent
    namespace is provided.
    """
    mock_proc = mocker.patch("greenkube.cli.report.get_processor")
    proc_inst = MagicMock()
    proc_inst.run.return_value = sample_combined_metrics
    mock_proc.return_value = proc_inst

    result = runner.invoke(app, ["report", "--namespace", "non-existent-ns"])

    # The CLI now exits cleanly with code 0 when a namespace has no data
    assert result.exit_code == 0
    # Reporter should not be called when no data exists for the requested namespace
    mock_reporter.report.assert_not_called()


def test_export_placeholder(mocker):
    """
    Tests the placeholder functionality of the `export` command.
    """
    # Patch the report module logger used when exporting
    mock_logger = mocker.patch("greenkube.cli.report.logger")
    # Ensure processor returns something so export runs
    mock_proc = mocker.patch("greenkube.cli.report.get_processor")
    proc_inst = MagicMock()
    proc_inst.run.return_value = [
        CombinedMetric(pod_name="p1", namespace="ns", total_cost=1.0, co2e_grams=1.0, pue=1.0, grid_intensity=1.0)
    ]
    mock_proc.return_value = proc_inst

    result = runner.invoke(app, ["report", "--output", "csv"])
    # Now exports actual data to disk; ensure exit_ok and that exporter logged the written path
    assert result.exit_code == 0
    called = any(
        "Successfully exported report" in str(call_args) or "Report exported to" in str(call_args)
        for call_args in mock_logger.info.call_args_list
    )
    assert called, f"Expected logger.info to be called with export message, got: {mock_logger.info.call_args_list}"


def test_recommend_calls_reporter_with_recommendations(mocker, mock_reporter, sample_combined_metrics):
    """Ensure `greenkube recommend` generates recommendations and calls reporter.report with recommendations."""
    # Patch the processor used by the recommend submodule
    mock_proc = mocker.patch("greenkube.cli.recommend.get_processor")
    proc_inst = MagicMock()
    proc_inst.run.return_value = sample_combined_metrics
    mock_proc.return_value = proc_inst

    # Patch Recommender to return a sample recommendation list (used in recommend submodule)
    sample_rec = mocker.patch("greenkube.cli.recommend.Recommender")
    rec_instance = sample_rec.return_value
    rec_instance.generate_zombie_recommendations.return_value = [
        mocker.MagicMock(
            pod_name="pod-y",
            namespace="monitoring",
            type=mocker.MagicMock(value="ZOMBIE_POD"),
            description="idle",
        )
    ]
    rec_instance.generate_rightsizing_recommendations.return_value = []

    # Patch recommend module's ConsoleReporter so the submodule uses the mock
    mock_console = mocker.patch("greenkube.cli.recommend.ConsoleReporter")
    console_inst = MagicMock()
    mock_console.return_value = console_inst

    result = runner.invoke(app, ["recommend"])
    assert result.exit_code == 0
    # ConsoleReporter.report_recommendations should be called with the list
    console_inst.report_recommendations.assert_called_once()
    args = console_inst.report_recommendations.call_args.args[0]
    assert isinstance(args, list)


def test_recommend_with_namespace_filter_no_data(mocker, mock_reporter, sample_combined_metrics):
    """When namespace filter yields no data, recommend exits cleanly."""
    mock_proc = mocker.patch("greenkube.cli.recommend.get_processor")
    proc_inst = MagicMock()
    proc_inst.run.return_value = sample_combined_metrics
    mock_proc.return_value = proc_inst

    result = runner.invoke(app, ["recommend", "--namespace", "non-existent-ns"])
    assert result.exit_code == 0
    # Reporter shouldn't be called
    mock_reporter.report.assert_not_called()


def test_report_range_today_no_results(mocker, mock_reporter):
    """Tests the ranged report path with no results (mocked)."""
    # Patch the processor.run_range to return empty results
    mock_proc = mocker.patch("greenkube.cli.report.get_processor")
    proc_inst = MagicMock()
    proc_inst.run_range.return_value = []
    mock_proc.return_value = proc_inst

    result = runner.invoke(app, ["report", "--last", "1d"])
    assert result.exit_code == 0
    # Reporter should have been called (with empty list)
    mock_reporter.report.assert_called_once()


def test_help_command_outputs_commands(capsys):
    """The dynamic help command should list available commands."""
    result = runner.invoke(app, ["--help"], env={"TERM": "dumb"})
    assert result.exit_code == 0
    assert "Usage: greenkube" in result.output


def test_unknown_command_shows_help():
    """When an unknown command is provided, the CLI should print help-like output."""
    result = runner.invoke(app, ["no-such-command"], env={"TERM": "dumb"})
    # Our wrapper exits with non-zero for unknown commands but prints available commands
    assert result.exit_code != 0
    assert "Usage: greenkube" in result.output


def test_report_range_monthly_flag(mocker, mock_reporter):
    """Tests that --monthly aggregates are accepted and reporter called."""
    mock_resp = MagicMock()
    # Provide a single sample with ISO timestamps spanning two months
    mock_resp.json.return_value = {
        "data": {
            "result": [
                {
                    "metric": {"namespace": "default", "pod": "p1"},
                    "values": [["1696118400", "0.1"]],
                },
            ]
        }
    }
    mock_resp.raise_for_status.return_value = None
    mocker.patch("requests.get", return_value=mock_resp)

    # Patch processor.run_range to return empty so reporter is called with []
    mock_proc = mocker.patch("greenkube.cli.report.get_processor")
    proc_inst = MagicMock()
    proc_inst.run_range.return_value = []
    mock_proc.return_value = proc_inst

    result = runner.invoke(app, ["report", "--monthly"])
    assert result.exit_code == 0
    mock_reporter.report.assert_called_once()


def test_report_range_yearly_flag(mocker, mock_reporter):
    """Tests that --yearly aggregates are accepted and reporter called."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": {
            "result": [
                {
                    "metric": {"namespace": "default", "pod": "p1"},
                    "values": [["1696118400", "0.1"]],
                },
            ]
        }
    }
    mock_resp.raise_for_status.return_value = None
    mocker.patch("requests.get", return_value=mock_resp)

    mock_proc = mocker.patch("greenkube.cli.report.get_processor")
    proc_inst = MagicMock()
    proc_inst.run_range.return_value = []
    mock_proc.return_value = proc_inst

    result = runner.invoke(app, ["report", "--yearly"])
    assert result.exit_code == 0
    mock_reporter.report.assert_called_once()
