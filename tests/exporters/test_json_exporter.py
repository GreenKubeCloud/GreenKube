# tests/exporters/test_json_exporter.py
import json

import pytest

from greenkube.exporters.json_exporter import JSONExporter


@pytest.mark.asyncio
async def test_json_exporter_empty_data(tmp_path):
    exporter = JSONExporter()
    out = tmp_path / "greenkube-report.json"
    await exporter.export([], str(out))
    assert out.exists()
    content = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(content, list)


@pytest.mark.asyncio
async def test_json_exporter_with_period(tmp_path):
    exporter = JSONExporter()
    out = tmp_path / "greenkube-report.json"
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
    content = json.loads(out.read_text(encoding="utf-8"))
    assert content[0]["namespace"] == "default"
