# tests/api/test_report.py
"""Tests for the report endpoints."""

import csv
import io
from unittest.mock import AsyncMock


class TestReportSummaryEndpoint:
    """Tests for GET /api/v1/report/summary."""

    def test_summary_returns_200_empty(self, client):
        """Should return 200 with zero values when no data."""
        response = client.get("/api/v1/report/summary")
        assert response.status_code == 200
        data = response.json()
        assert data["total_rows"] == 0
        assert data["total_co2e_grams"] == 0.0
        assert data["total_embodied_co2e_grams"] == 0.0
        assert data["total_co2e_all_scopes"] == 0.0
        assert data["total_cost"] == 0.0
        assert data["total_energy_joules"] == 0.0
        assert data["unique_pods"] == 0
        assert data["unique_namespaces"] == 0

    def test_summary_returns_correct_totals(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should compute correct totals from metrics."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/summary")
        assert response.status_code == 200
        data = response.json()
        assert data["total_rows"] == 2
        assert abs(data["total_co2e_grams"] - 5.7) < 0.01
        assert abs(data["total_embodied_co2e_grams"] - 0.17) < 0.01
        assert abs(data["total_co2e_all_scopes"] - (5.7 + 0.17)) < 0.01
        assert abs(data["total_cost"] - 0.017) < 0.001
        assert abs(data["total_energy_joules"] - 20000.0) < 0.01
        assert data["unique_pods"] == 2
        assert data["unique_namespaces"] == 2

    def test_summary_filter_by_namespace(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should filter metrics by namespace before computing totals."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/summary?namespace=default")
        assert response.status_code == 200
        data = response.json()
        assert data["total_rows"] == 1
        assert data["unique_namespaces"] == 1

    def test_summary_with_aggregate(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should aggregate metrics when aggregate=true."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/summary?aggregate=true&granularity=daily")
        assert response.status_code == 200
        data = response.json()
        assert data["total_rows"] >= 1

    def test_summary_invalid_last_returns_400(self, client):
        """Should return 400 for an invalid last parameter."""
        response = client.get("/api/v1/report/summary?last=invalid_range")
        assert response.status_code == 400

    def test_summary_invalid_granularity_returns_400(self, client):
        """Should return 400 for an invalid granularity value."""
        response = client.get("/api/v1/report/summary?aggregate=true&granularity=quarterly")
        assert response.status_code == 400

    def test_summary_valid_last(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Should accept a valid last parameter."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/summary?last=7d")
        assert response.status_code == 200

    def test_summary_supports_ytd_window(self, client, mock_combined_metrics_repo):
        """YTD should resolve to January 1st of the current UTC year."""
        mock_combined_metrics_repo.read_combined_metrics_smart = AsyncMock(return_value=[])

        response = client.get("/api/v1/report/summary?last=ytd")

        assert response.status_code == 200
        kwargs = mock_combined_metrics_repo.read_combined_metrics_smart.await_args.kwargs
        start = kwargs["start_time"]
        end = kwargs["end_time"]
        assert start.year == end.year
        assert start.month == 1
        assert start.day == 1
        assert start.hour == 0
        assert start.minute == 0
        assert start.second == 0
        assert start.tzinfo is not None


class TestReportExportEndpoint:
    """Tests for GET /api/v1/report/export."""

    def test_export_csv_returns_200(self, client):
        """Should return 200 with CSV content type for an empty dataset."""
        response = client.get("/api/v1/report/export?format=csv")
        assert response.status_code == 200
        assert "text/csv" in response.headers.get("content-type", "")

    def test_export_json_returns_200(self, client):
        """Should return 200 with JSON content type for an empty dataset."""
        response = client.get("/api/v1/report/export?format=json")
        assert response.status_code == 200
        assert "application/json" in response.headers.get("content-type", "")

    def test_export_csv_content(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """CSV export should contain headers and data rows."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/export?format=csv")
        assert response.status_code == 200
        content = response.text
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        assert len(rows) == 2
        assert "pod_name" in reader.fieldnames
        assert "co2e_grams" in reader.fieldnames
        assert "total_cost" in reader.fieldnames
        assert rows[0]["pod_name"] == "nginx-abc123"

    def test_export_json_content(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """JSON export should be a valid JSON array with correct data."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/export?format=json")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["pod_name"] == "nginx-abc123"

    def test_export_invalid_format_returns_400(self, client):
        """Should return 400 for an unsupported format."""
        response = client.get("/api/v1/report/export?format=xml")
        assert response.status_code == 400

    def test_export_invalid_granularity_returns_400(self, client):
        """Should return 400 for an invalid granularity."""
        response = client.get("/api/v1/report/export?aggregate=true&granularity=quarterly")
        assert response.status_code == 400

    def test_export_csv_content_disposition(self, client):
        """Response should include a Content-Disposition header for download."""
        response = client.get("/api/v1/report/export?format=csv")
        assert "attachment" in response.headers.get("content-disposition", "")
        assert ".csv" in response.headers.get("content-disposition", "")

    def test_export_json_content_disposition(self, client):
        """Response should include a Content-Disposition header for download."""
        response = client.get("/api/v1/report/export?format=json")
        assert "attachment" in response.headers.get("content-disposition", "")
        assert ".json" in response.headers.get("content-disposition", "")

    def test_export_with_namespace_filter(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """CSV export should only contain rows for the requested namespace."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/export?format=csv&namespace=default")
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 1
        assert rows[0]["namespace"] == "default"

    def test_export_with_aggregate_daily(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Aggregated export should group rows correctly."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/export?format=json&aggregate=true&granularity=daily")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_export_empty_csv(self, client):
        """Empty dataset should return an empty CSV (no rows, no header)."""
        response = client.get("/api/v1/report/export?format=csv")
        assert response.status_code == 200
        assert response.text == ""

    def test_export_empty_json(self, client):
        """Empty dataset should return an empty JSON array."""
        response = client.get("/api/v1/report/export?format=json")
        assert response.status_code == 200
        assert response.json() == []

    def test_export_default_format_is_csv(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        """Default format should be CSV when format param is omitted."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/export")
        assert response.status_code == 200
        assert "text/csv" in response.headers.get("content-type", "")

    def test_export_supports_ytd_window(self, client, mock_combined_metrics_repo):
        """YTD exports should use January 1st through now as the time range."""
        mock_combined_metrics_repo.read_combined_metrics_smart = AsyncMock(return_value=[])

        response = client.get("/api/v1/report/export?format=json&last=ytd")

        assert response.status_code == 200
        kwargs = mock_combined_metrics_repo.read_combined_metrics_smart.await_args.kwargs
        start = kwargs["start_time"]
        end = kwargs["end_time"]
        assert start.year == end.year
        assert start.month == 1
        assert start.day == 1
        assert start.hour == 0
        assert start.minute == 0
        assert start.second == 0
        assert start.tzinfo is not None
