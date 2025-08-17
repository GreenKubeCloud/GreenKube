# tests/test_cli.py
"""
Unit tests for the GreenKube Command-Line Interface (CLI).
"""
import pytest
from typer.testing import CliRunner
from unittest.mock import MagicMock, ANY

# Import the Typer app instance from your CLI module
from greenkube.cli import app
from greenkube.models.metrics import CombinedMetric

# Create a CliRunner instance to invoke the commands
runner = CliRunner()

def test_report_success(mocker):
    """
    Tests that the `greenkube report` command correctly calls the reporter with data.
    """
    # 1. Arrange: Patch the ConsoleReporter to intercept its creation and methods.
    mock_reporter_class = mocker.patch('greenkube.cli.ConsoleReporter')
    mock_reporter_instance = MagicMock()
    mock_reporter_class.return_value = mock_reporter_instance

    # 2. Act
    result = runner.invoke(app, ["report"])

    # 3. Assert
    assert result.exit_code == 0, "CLI should exit without errors"
    
    # FIX: Check for a call with the keyword argument 'data'
    mock_reporter_instance.report.assert_called_once_with(data=ANY)

    # We can now safely inspect the data that was passed via the keyword argument.
    reported_data = mock_reporter_instance.report.call_args.kwargs['data']
    assert len(reported_data) > 0 # Check that some data was passed
    assert isinstance(reported_data[0], CombinedMetric)


def test_report_with_namespace_filter_success(mocker):
    """
    Tests that the CLI correctly filters data before passing it to the reporter.
    """
    # 1. Arrange
    mock_reporter_class = mocker.patch('greenkube.cli.ConsoleReporter')
    mock_reporter_instance = MagicMock()
    mock_reporter_class.return_value = mock_reporter_instance

    # 2. Act
    result = runner.invoke(app, ["report", "--namespace", "security"])

    # 3. Assert
    assert result.exit_code == 0
    assert "Filtering results for namespace: security" in result.stdout
    
    # FIX: Check for a call with the keyword argument 'data'
    mock_reporter_instance.report.assert_called_once_with(data=ANY)
    
    # CRITICAL: Inspect the data that was passed to the mocked reporter.
    reported_data = mock_reporter_instance.report.call_args.kwargs['data']

    # Check that only data for the 'security' namespace was reported.
    assert len(reported_data) == 1
    assert reported_data[0].namespace == "security"
    assert reported_data[0].pod_name == "auth-service-fgh"


def test_report_namespace_not_found():
    """
    Tests that the CLI exits gracefully with a warning when a non-existent
    namespace is provided.
    """
    # 1. Arrange & 2. Act
    result = runner.invoke(app, ["report", "--namespace", "non-existent-ns"])

    # 3. Assert
    assert result.exit_code != 0, "CLI should exit with a non-zero code for errors"
    assert "WARN: No data found for namespace 'non-existent-ns'" in result.stdout


def test_export_placeholder():
    """
    Tests the placeholder functionality of the `export` command.
    """
    # 1. Arrange & 2. Act
    result = runner.invoke(app, ["export"])

    # 3. Assert
    assert result.exit_code == 0
    assert "Placeholder: Exporting data in csv format to report.csv" in result.stdout
