# tests/exporters/test_csv_exporter.py

from greenkube.exporters.csv_exporter import CSVExporter


def test_csv_exporter_empty_data(tmp_path):
    exporter = CSVExporter()
    out = tmp_path / "greenkube-report.csv"
    exporter.export([], str(out))
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    # Empty exporter still writes header or empty file
    assert content is not None


def test_csv_exporter_with_period(tmp_path):
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
    exporter.export(data, str(out))
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "namespace" in content
    assert "default" in content
