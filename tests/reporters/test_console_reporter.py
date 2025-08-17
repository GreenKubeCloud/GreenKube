# tests/reporters/test_console_reporter.py
"""
Unit tests for the ConsoleReporter class.
"""
import pytest
from unittest.mock import MagicMock, call

# We need to mock Table and Console from the reporter's namespace
from greenkube.reporters.console_reporter import ConsoleReporter
from greenkube.models.metrics import CombinedMetric

def test_console_reporter_with_data(mocker):
    """
    Tests that the ConsoleReporter correctly constructs and prints a rich Table.
    This test mocks the Table and Console objects to verify their interactions.
    """
    # 1. Arrange: Mock both Console and Table in the reporter's module
    mock_console_class = mocker.patch('greenkube.reporters.console_reporter.Console')
    mock_table_class = mocker.patch('greenkube.reporters.console_reporter.Table')

    # Create mock instances that will be returned when the classes are instantiated
    mock_console_instance = MagicMock()
    mock_table_instance = MagicMock()
    mock_console_class.return_value = mock_console_instance
    mock_table_class.return_value = mock_table_instance

    mock_data = [
        CombinedMetric(
            namespace="e-commerce",
            pod_name="backend-xyz",
            total_cost=15.75,
            co2e_grams=250.5,
            pue=1.5,
            grid_intensity=400.0
        ),
        CombinedMetric(
            namespace="security",
            pod_name="auth-service-fgh",
            total_cost=5.10,
            co2e_grams=80.2,
            pue=1.5,
            grid_intensity=400.0
        )
    ]
    reporter = ConsoleReporter()

    # 2. Act: Call the report method.
    reporter.report(mock_data)

    # 3. Assert
    # Assert that the Table was instantiated with the correct title and styles
    mock_table_class.assert_called_once_with(
        show_header=True,
        header_style="bold magenta",
        title="GreenKube FinGreenOps Report"
    )

    # Assert that the columns were added correctly to the table instance
    expected_column_calls = [
        call("Namespace", style="dim"),
        call("Pod Name"),
        call("Total Cost ($)", justify="right"),
        call("CO2e (grams)", justify="right"),
        call("PUE", justify="right"),
        call("Grid Intensity (gCO2e/kWh)", justify="right"),
    ]
    mock_table_instance.add_column.assert_has_calls(expected_column_calls, any_order=False)

    # Assert that the rows were added with the correct, formatted data
    expected_row_calls = [
        call("e-commerce", "backend-xyz", "15.7500", "250.5000", "1.50", "400.0"),
        call("security", "auth-service-fgh", "5.1000", "80.2000", "1.50", "400.0"),
    ]
    mock_table_instance.add_row.assert_has_calls(expected_row_calls, any_order=False)

    # Assert that the final table object was printed to the console
    mock_console_instance.print.assert_called_once_with(mock_table_instance)


def test_console_reporter_with_no_data(mocker):
    """
    Tests that the ConsoleReporter handles an empty data list gracefully,
    creating a table with headers but adding no rows.
    """
    # 1. Arrange
    mock_console_class = mocker.patch('greenkube.reporters.console_reporter.Console')
    mock_table_class = mocker.patch('greenkube.reporters.console_reporter.Table')
    mock_console_instance = MagicMock()
    mock_table_instance = MagicMock()
    mock_console_class.return_value = mock_console_instance
    mock_table_class.return_value = mock_table_instance

    mock_data = []
    reporter = ConsoleReporter()

    # 2. Act
    reporter.report(mock_data)

    # 3. Assert
    # Check that a table was still created with the correct title
    mock_table_class.assert_called_once_with(
        show_header=True,
        header_style="bold magenta",
        title="GreenKube FinGreenOps Report"
    )

    # Check that columns were added
    assert mock_table_instance.add_column.call_count == 6

    # CRITICAL: Check that add_row was NEVER called
    mock_table_instance.add_row.assert_not_called()

    # Check that the empty table was still printed
    mock_console_instance.print.assert_called_once_with(mock_table_instance)
