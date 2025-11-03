import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import sys
sys.path.insert(0, 'src')

import greenkube.cli as cli
from greenkube.models.metrics import CombinedMetric


def make_dummy_processor(return_items=None):
    proc = MagicMock()
    proc.run = MagicMock(return_value=return_items or [])
    proc.estimator = MagicMock()
    proc.calculator = MagicMock()
    proc.repository = MagicMock()
    proc.node_collector = MagicMock()
    proc.pod_collector = MagicMock()
    proc.opencost_collector = MagicMock()
    return proc


def test_report_without_flags_calls_processor_and_reports(monkeypatch):
    # Arrange: create dummy combined data
    items = [CombinedMetric(pod_name='p1', namespace='ns1', total_cost=1.0, co2e_grams=10.0, joules=100.0),
             CombinedMetric(pod_name='p2', namespace='ns2', total_cost=2.0, co2e_grams=20.0, joules=200.0)]

    dummy_proc = make_dummy_processor(return_items=items)
    monkeypatch.setattr(cli, 'get_processor', lambda: dummy_proc)

    reported = []

    class DummyReporter:
        def report(self, data):
            # Reporter.report now accepts only the data list
            reported.append(list(data))

    monkeypatch.setattr(cli, 'ConsoleReporter', lambda: DummyReporter())

    # Act
    cli.report()

    # Assert
    assert len(reported) == 1
    assert reported[0] == items


def test_report_with_range_delegates_to_report_range(monkeypatch):
    # Arrange: patch report_range to capture calls
    called = {}

    def fake_report_range(**kwargs):
        called['args'] = kwargs
        return None

    monkeypatch.setattr(cli, 'report_range', fake_report_range)

    # Act
    cli.report(hours=2)

    # Assert
    assert 'args' in called
    assert called['args'].get('hours') == 2


def test_recommend_generates_and_reports(monkeypatch):
    # Arrange: create dummy combined data and dummy recommendations
    items = [CombinedMetric(pod_name='p1', namespace='ns1', total_cost=1.0, co2e_grams=10.0, joules=100.0)]

    dummy_proc = make_dummy_processor(return_items=items)
    monkeypatch.setattr(cli, 'get_processor', lambda: dummy_proc)

    # Dummy recommender that returns some recommendations
    class DummyRec:
        def __init__(self, pod_name, namespace):
            self.pod_name = pod_name
            self.namespace = namespace
            self.type = None
            self.description = 'desc'

    dummy_recommender = MagicMock()
    dummy_recommender.generate_zombie_recommendations = MagicMock(return_value=[DummyRec('p1', 'ns1')])
    dummy_recommender.generate_rightsizing_recommendations = MagicMock(return_value=[])
    monkeypatch.setattr(cli, 'Recommender', lambda: dummy_recommender)

    reported = []
    class DummyReporter2:
        def report_recommendations(self, recommendations):
            reported.append(list(recommendations))

    monkeypatch.setattr(cli, 'ConsoleReporter', lambda: DummyReporter2())

    # Act: should not raise
    cli.recommend()

    # Assert
    assert len(reported) == 1
    assert reported[0][0].pod_name == 'p1'


def test_report_range_with_output_exports(monkeypatch, tmp_path):
    # Arrange: create dummy combined data
    items = [CombinedMetric(pod_name='p1', namespace='ns1', total_cost=1.0, co2e_grams=10.0, joules=100.0)]
    dummy_proc = make_dummy_processor(return_items=items)
    monkeypatch.setattr(cli, 'get_processor', lambda: dummy_proc)

    # Patch exporters to write to tmp_path and capture call
    written = {}
    class DummyExporter:
        DEFAULT_FILENAME = 'greenkube-report.csv'
        def export(self, data, path=None):
            written['path'] = path
            return path

    import greenkube.exporters.csv_exporter as csv_mod
    monkeypatch.setattr(csv_mod, 'CSVExporter', DummyExporter)

    # Act: ask for monthly range and output csv (shortcut)
    cli.report_range(monthly=True, output='csv')

    # Assert: exporter was invoked and wrote to data folder path
    assert 'path' in written
    assert written['path'].endswith('greenkube-report.csv')

