from unittest.mock import AsyncMock, MagicMock

from typer.testing import CliRunner

import greenkube.cli.recommend as recommend_mod
import greenkube.cli.report as report_mod
from greenkube.cli import app
from greenkube.models.metrics import CombinedMetric


def make_dummy_processor(return_items=None):
    proc = MagicMock()
    proc.run = AsyncMock(return_value=return_items or [])
    proc.run_range = AsyncMock(return_value=return_items or [])
    proc.close = AsyncMock()
    proc.estimator = MagicMock()
    proc.calculator = MagicMock()
    proc.repository = MagicMock()
    proc.node_collector = MagicMock()
    proc.pod_collector = MagicMock()
    proc.opencost_collector = MagicMock()
    return proc


def test_report_without_flags_calls_processor_and_reports(monkeypatch):
    # Arrange: create dummy combined data
    items = [
        CombinedMetric(
            pod_name="p1",
            namespace="ns1",
            total_cost=1.0,
            co2e_grams=10.0,
            joules=100.0,
        ),
        CombinedMetric(
            pod_name="p2",
            namespace="ns2",
            total_cost=2.0,
            co2e_grams=20.0,
            joules=200.0,
        ),
    ]

    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=items)
    monkeypatch.setattr(report_mod, "get_repository", lambda: dummy_repo)

    reported = []

    class DummyReporter:
        def report(self, data):
            # Reporter.report now accepts only the data list
            reported.append(list(data))

    monkeypatch.setattr(report_mod, "ConsoleReporter", lambda: DummyReporter())

    # Act via CLI runner
    runner = CliRunner()
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0

    # Assert
    assert len(reported) == 1
    assert reported[0] == items


def test_report_with_range_delegates_to_report_range(monkeypatch):
    # Arrange: patch the repository to capture calls
    mock_repo = MagicMock()
    mock_repo.read_combined_metrics = AsyncMock(return_value=[])
    monkeypatch.setattr(report_mod, "get_repository", lambda: mock_repo)

    runner = CliRunner()
    result = runner.invoke(app, ["report", "--last", "2h"])  # trigger range path
    assert result.exit_code == 0
    # Assert read_combined_metrics was called
    mock_repo.read_combined_metrics.assert_called()


def test_recommend_generates_and_reports(monkeypatch):
    # Arrange: create dummy combined data and dummy recommendations
    items = [
        CombinedMetric(
            pod_name="p1",
            namespace="ns1",
            total_cost=1.0,
            co2e_grams=10.0,
            joules=100.0,
        )
    ]

    dummy_proc = make_dummy_processor(return_items=items)
    monkeypatch.setattr(recommend_mod, "get_processor", lambda: dummy_proc)

    # Dummy recommender that returns some recommendations
    class DummyRec:
        def __init__(self, pod_name, namespace):
            self.pod_name = pod_name
            self.namespace = namespace
            self.type = None
            self.description = "desc"

    dummy_recommender = MagicMock()
    dummy_recommender.generate_zombie_recommendations = MagicMock(return_value=[DummyRec("p1", "ns1")])
    dummy_recommender.generate_rightsizing_recommendations = MagicMock(return_value=[])
    monkeypatch.setattr(recommend_mod, "Recommender", lambda: dummy_recommender)

    reported = []

    class DummyReporter2:
        def report_recommendations(self, recommendations):
            reported.append(list(recommendations))

    monkeypatch.setattr(recommend_mod, "ConsoleReporter", lambda: DummyReporter2())

    runner = CliRunner()
    result = runner.invoke(app, ["recommend"])  # Act
    assert result.exit_code == 0

    # Assert
    assert len(reported) == 1
    assert reported[0][0].pod_name == "p1"


def test_report_range_with_output_exports(monkeypatch, tmp_path):
    # Arrange: create dummy combined data
    items = [
        CombinedMetric(
            pod_name="p1",
            namespace="ns1",
            total_cost=1.0,
            co2e_grams=10.0,
            joules=100.0,
        )
    ]
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=items)
    monkeypatch.setattr(report_mod, "get_repository", lambda: dummy_repo)

    # Patch exporters to write to tmp_path and capture call
    written = {}

    class DummyExporter:
        DEFAULT_FILENAME = "greenkube-report.csv"

        def export(self, data, path=None):
            written["path"] = path
            return path

    monkeypatch.setattr(report_mod, "CSVExporter", DummyExporter)

    # Act: ask for monthly range and output csv (shortcut via CLI)
    runner = CliRunner()
    result = runner.invoke(app, ["report", "--monthly", "--output", "csv"])
    assert result.exit_code == 0

    # Assert: exporter was invoked and wrote to data folder path
    assert "path" in written
    assert written["path"].endswith("greenkube-report.csv")
