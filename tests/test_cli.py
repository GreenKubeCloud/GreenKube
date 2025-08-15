# tests/test_cli.py
"""
Unit tests for the GreenKube Command-Line Interface (CLI).
"""
import pytest
from typer.testing import CliRunner

# Import the Typer app instance from your CLI module
from greenkube.cli import app

# Create a CliRunner instance to invoke the commands
runner = CliRunner()

def test_report_success():
    """
    Tests that the `greenkube report` command runs successfully
    and contains the expected table headers and data.
    """
    # 1. Arrange & 2. Act
    result = runner.invoke(app, ["report"])

    # 3. Assert
    assert result.exit_code == 0, "CLI should exit without errors"
    assert "Initializing GreenKube" in result.stdout, "Should show initialization message"
    assert "Generating report..." in result.stdout, "Should show report generation message"
    
    # Check for table headers and specific, predictable pod names
    assert "Pod Name" in result.stdout
    assert "Total Cost ($)" in result.stdout
    assert "CO2e (grams)" in result.stdout
    assert "backend-xyz" in result.stdout  # A known pod from mocked data
    assert "e-commerce" in result.stdout # A known namespace
    print("\nTestCLI (report): All assertions passed! ✅")


def test_report_with_namespace_filter_success():
    """
    Tests that the `greenkube report --namespace` command correctly
    filters the output to the specified namespace.
    """
    # 1. Arrange & 2. Act
    result = runner.invoke(app, ["report", "--namespace", "security"])

    # 3. Assert
    assert result.exit_code == 0
    assert "Filtering results for namespace: security" in result.stdout
    
    # Check that only the correct pod is displayed
    assert "auth-service-fgh" in result.stdout
    
    # Check that pods from other namespaces are NOT present
    assert "backend-xyz" not in result.stdout
    assert "e-commerce" not in result.stdout
    print("\nTestCLI (report --namespace): All assertions passed! ✅")


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
    print("\nTestCLI (report namespace not found): All assertions passed! ✅")


def test_export_placeholder():
    """
    Tests the placeholder functionality of the `export` command.
    """
    # 1. Arrange & 2. Act
    result = runner.invoke(app, ["export"])

    # 3. Assert
    assert result.exit_code == 0
    assert "Placeholder: Exporting data in csv format to report.csv" in result.stdout
    print("\nTestCLI (export): All assertions passed! ✅")