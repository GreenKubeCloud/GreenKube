# tests/reporters/test_console_reporter.py
"""
Unit tests for the ConsoleReporter class.
"""
import pytest
from unittest.mock import MagicMock, call

# We need to mock Table and Console from the reporter's namespace
from greenkube.reporters.console_reporter import ConsoleReporter
from greenkube.models.metrics import CombinedMetric
from greenkube.models.metrics import Recommendation, RecommendationType

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
    # Assert Table was instantiated and includes our title and header style
    assert mock_table_class.call_count == 1
    call_kwargs = mock_table_class.call_args.kwargs
    assert call_kwargs.get('title') == "GreenKube FinGreenOps Report"
    assert call_kwargs.get('header_style') == "bold magenta"

    # Assert that the columns were added correctly to the table instance
    expected_column_calls = [
        call("Pod Name", style="cyan"),
        call("Namespace", style="cyan"),
        call("Total Cost ($)", style="green", justify="right"),
        call("CO2e (g)", style="red", justify="right"),
        call("Energy (Joules)", style="yellow", justify="right"),
        call("CPU Req (m)", style="blue", justify="right"),
        call("Mem Req (Mi)", style="blue", justify="right"),
        call("Grid Intensity (g/kWh)", style="dim", justify="right"),
        call("PUE", style="dim", justify="right"),
    ]
    mock_table_instance.add_column.assert_has_calls(expected_column_calls, any_order=False)

    # Assert that the rows were added with the correct, formatted data
    # For our sample CombinedMetric defaults, joules/cpu/mem/intensity may be 0
    expected_row_calls = [
        call("backend-xyz", "e-commerce", "15.7500", "250.50", "0", "0", "0.0", "400.00", "1.50"),
        call("auth-service-fgh", "security", "5.1000", "80.20", "0", "0", "0.0", "400.00", "1.50"),
    ]
    mock_table_instance.add_row.assert_has_calls(expected_row_calls, any_order=False)

    # Assert that the final table object was printed to the console
    mock_console_instance.print.assert_called_once_with(mock_table_instance)


def test_console_reporter_includes_cpu_and_memory(mocker):
    """Ensure report shows CPU and memory columns and formats memory in Mi."""
    mock_console_class = mocker.patch('greenkube.reporters.console_reporter.Console')
    mock_table_class = mocker.patch('greenkube.reporters.console_reporter.Table')
    mock_console_instance = MagicMock()
    mock_table_instance = MagicMock()
    mock_console_class.return_value = mock_console_instance
    mock_table_class.return_value = mock_table_instance

    data = [
        CombinedMetric(
            namespace="ns1",
            pod_name="pod-1",
            total_cost=1.0,
            co2e_grams=10.0,
            pue=1.1,
            grid_intensity=200.0,
            joules=1000,
            cpu_request=250,
            memory_request=50 * 1024 * 1024, # 50 Mi
        )
    ]

    reporter = ConsoleReporter()
    reporter.report(data)

    # Table should be instantiated with the title as defined
    mock_table_class.assert_called_once()
    # Assert that add_row was called with memory formatted in Mi (50.0)
    # We cannot know exact call order for args since Table is mocked; inspect add_row calls
    added_rows = mock_table_instance.add_row.call_args_list
    assert added_rows, "Expected at least one row to be added"
    # Find the row containing 'pod-1'
    found = False
    for call_args in added_rows:
        args = call_args.args
        if 'pod-1' in args:
            # memory is at position 6 (0-based): pod, ns, cost, co2e, joules, cpu, mem, intensity, pue
            mem_str = args[6]
            assert mem_str == '50.0' or mem_str.startswith('50.0')
            found = True
    assert found, "Row for pod-1 not found in calls"


def test_console_reporter_recommendations(mocker):
    """Ensure recommendations are printed properly in a second table."""
    mock_console_class = mocker.patch('greenkube.reporters.console_reporter.Console')
    mock_table_class = mocker.patch('greenkube.reporters.console_reporter.Table')
    mock_console_instance = MagicMock()
    mock_table_instance = MagicMock()
    mock_console_class.return_value = mock_console_instance
    mock_table_class.return_value = mock_table_instance

    recs = [
        Recommendation(pod_name='z', namespace='default', type=RecommendationType.ZOMBIE_POD, description='idle'),
        Recommendation(pod_name='o', namespace='prod', type=RecommendationType.RIGHTSIZING_CPU, description='rightsizing')
    ]

    reporter = ConsoleReporter()
    reporter.report_recommendations(recs)

    # Should have created a table for recommendations and printed it
    mock_table_class.assert_called()
    mock_console_instance.print.assert_called()


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
    # Reporter prints a short warning and does not create a table
    mock_table_class.assert_not_called()
    mock_console_instance.print.assert_called_once_with("No data to report.", style="yellow")
