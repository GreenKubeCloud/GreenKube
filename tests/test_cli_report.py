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
        def report(self, data, recommendations=None):
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

