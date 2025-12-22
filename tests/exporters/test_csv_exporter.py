# tests/exporters/test_csv_exporter.py

import pytest

from greenkube.exporters.csv_exporter import CSVExporter


@pytest.mark.asyncio
async def test_csv_exporter_empty_data(tmp_path):
    exporter = CSVExporter()
    out = tmp_path / "greenkube-report.csv"
    await exporter.export([], str(out))
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    # Empty exporter still writes header or empty file
    assert content is not None


@pytest.mark.asyncio
async def test_csv_exporter_with_period(tmp_path):
    exporter = CSVExporter()
    out = tmp_path / "greenkube-report.csv"
    data = [
        {
            "namespace": "default",
            "timestamp": "2020-01-01T00:00:00Z",
            "cpu": 0.5,
            "memory": 128,
        },
    ]
    await exporter.export(data, str(out))
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "namespace" in content
    assert "default" in content


@pytest.mark.asyncio
async def test_csv_exporter_injection(tmp_path):
    exporter = CSVExporter()
    out = tmp_path / "greenkube-report-injection.csv"
    data = [
        {
            "pod_name": "=cmd|' /C calc'!A0",
            "namespace": "default",
        },
        {
            "pod_name": "normal-pod",
            "namespace": "+bad-namespace",
        },
    ]
    await exporter.export(data, str(out))

    content = out.read_text(encoding="utf-8")
    # Verify escaping
    assert "'=cmd|' /C calc'!A0" in content
    assert "'+bad-namespace" in content
    assert "normal-pod" in content
