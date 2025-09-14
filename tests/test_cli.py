# tests/test_cli.py
"""
Unit tests for the GreenKube Command-Line Interface (CLI).
"""
import pytest
from typer.testing import CliRunner
from unittest.mock import MagicMock, ANY

from greenkube.cli import app
from greenkube.models.metrics import CombinedMetric

runner = CliRunner()

@pytest.fixture
def mock_reporter(mocker):
    """
    Fixture to patch ConsoleReporter and provide a mock instance.
    """
    mock_reporter_class = mocker.patch('greenkube.cli.ConsoleReporter')
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
            grid_intensity=200.0
        ),
        CombinedMetric(
            pod_name="pod-y",
            namespace="monitoring",
            total_cost=2.0,
            co2e_grams=200.0,
            pue=1.6,
            grid_intensity=210.0
        ),
    ]

def test_report_success(mocker, mock_reporter, sample_combined_metrics):
    """
    Tests that the `greenkube report` command correctly calls the reporter with data.
    """
    # Patch the processor's run method to return controlled data
    mocker.patch('greenkube.cli.DataProcessor.run', return_value=sample_combined_metrics)

    result = runner.invoke(app, ["report"])

    assert result.exit_code == 0, "CLI should exit without errors"
    mock_reporter.report.assert_called_once_with(data=ANY)
    reported_data = mock_reporter.report.call_args.kwargs['data']
    assert isinstance(reported_data, list)
    assert all(isinstance(item, CombinedMetric) for item in reported_data)
    assert len(reported_data) == 2

def test_report_with_namespace_filter_success(mocker, mock_reporter, sample_combined_metrics):
    """
    Tests that the CLI correctly filters data before passing it to the reporter.
    """
    mocker.patch('greenkube.cli.DataProcessor.run', return_value=sample_combined_metrics)

    result = runner.invoke(app, ["report", "--namespace", "monitoring"])

    assert result.exit_code == 0
    assert "Filtering results for namespace: monitoring" in result.stdout
    mock_reporter.report.assert_called_once_with(data=ANY)
    reported_data = mock_reporter.report.call_args.kwargs['data']
    # Only the metric with namespace "monitoring" should be present
    assert len(reported_data) == 1
    assert reported_data[0].namespace == "monitoring"

def test_report_namespace_not_found(mocker, mock_reporter, sample_combined_metrics):
    """
    Tests that the CLI exits gracefully with a warning when a non-existent
    namespace is provided.
    """
    mocker.patch('greenkube.cli.DataProcessor.run', return_value=sample_combined_metrics)

    result = runner.invoke(app, ["report", "--namespace", "non-existent-ns"])

    assert result.exit_code != 0, "CLI should exit with a non-zero code for errors"
    assert "WARN: No data found for namespace 'non-existent-ns'" in result.stdout

def test_export_placeholder():
    """
    Tests the placeholder functionality of the `export` command.
    """
    result = runner.invoke(app, ["export"])
    assert result.exit_code == 0
    assert "Placeholder: Exporting data in csv format to report.csv" in result.stdout
