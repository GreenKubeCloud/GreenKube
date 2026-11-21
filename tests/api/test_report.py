# tests/api/test_report.py
"""Tests for the report endpoints."""

import csv
import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from greenkube.models.metrics import CombinedMetric


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
        """aggregate=true uses SQL-level grouped row count — no CombinedMetric loaded."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        response = client.get("/api/v1/report/summary?aggregate=true&granularity=daily")
        assert response.status_code == 200
        data = response.json()
        assert data["total_rows"] >= 0

    def test_summary_aggregate_uses_sql_not_python_for_large_ranges(self, client, mock_combined_metrics_repo):
        """aggregate=true must return 200 even for wide ranges — no Python-load cap."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[])
        response = client.get("/api/v1/report/summary?aggregate=true&last=365d")
        assert response.status_code == 200

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
        mock_combined_metrics_repo.aggregate_summary = AsyncMock(
            return_value={
                "total_co2e_grams": 0.0,
                "total_embodied_co2e_grams": 0.0,
                "total_cost": 0.0,
                "total_energy_joules": 0.0,
                "pod_count": 0,
                "namespace_count": 0,
                "row_count": 0,
            }
        )

        response = client.get("/api/v1/report/summary?last=ytd")

        assert response.status_code == 200
        assert mock_combined_metrics_repo.aggregate_summary.await_args is not None
        kwargs = mock_combined_metrics_repo.aggregate_summary.await_args.kwargs
        start = kwargs["start_time"]
        end = kwargs["end_time"]
        assert start.year == end.year
        assert start.month == 1
        assert start.day == 1
        assert start.hour == 0
        assert start.minute == 0
        assert start.second == 0
        assert start.tzinfo is not None

    def test_summary_supports_custom_date_range(self, client, mock_combined_metrics_repo):
        """Custom date ranges should use explicit UTC day boundaries."""
        mock_combined_metrics_repo.aggregate_summary = AsyncMock(
            return_value={
                "total_co2e_grams": 0.0,
                "total_embodied_co2e_grams": 0.0,
                "total_cost": 0.0,
                "total_energy_joules": 0.0,
                "pod_count": 0,
                "namespace_count": 0,
                "row_count": 0,
            }
        )

        response = client.get("/api/v1/report/summary?start=2025-01-02&end=2025-01-05")

        assert response.status_code == 200
        assert mock_combined_metrics_repo.aggregate_summary.await_args is not None
        kwargs = mock_combined_metrics_repo.aggregate_summary.await_args.kwargs
        assert kwargs["start_time"] == datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
        assert kwargs["end_time"] == datetime(2025, 1, 5, 23, 59, 59, 999999, tzinfo=timezone.utc)

    def test_summary_rejects_partial_custom_date_range(self, client):
        """Custom report ranges require both start and end."""
        response = client.get("/api/v1/report/summary?start=2025-01-02")

        assert response.status_code == 400

    def test_summary_supports_selected_years(self, client, mock_combined_metrics_repo):
        """Selected years should query aggregate_summary once per calendar year."""
        mock_combined_metrics_repo.aggregate_summary = AsyncMock(
            return_value={
                "total_co2e_grams": 0.0,
                "total_embodied_co2e_grams": 0.0,
                "total_cost": 0.0,
                "total_energy_joules": 0.0,
                "pod_count": 0,
                "namespace_count": 0,
                "row_count": 0,
            }
        )

        response = client.get("/api/v1/report/summary?years=2024&years=2026")

        assert response.status_code == 200
        assert mock_combined_metrics_repo.aggregate_summary.await_count == 2
        first_call = mock_combined_metrics_repo.aggregate_summary.await_args_list[0].kwargs
        second_call = mock_combined_metrics_repo.aggregate_summary.await_args_list[1].kwargs
        assert first_call["start_time"] == datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert first_call["end_time"] == datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        assert second_call["start_time"] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_summary_invalid_group_by_returns_400(self, client):
        """Only supported report grouping dimensions are accepted."""
        response = client.get("/api/v1/report/summary?aggregate=true&group_by=node")

        assert response.status_code == 400


class TestReportYearsEndpoint:
    """Tests for GET /api/v1/report/years."""

    def test_years_returns_available_metric_years(self, client, mock_combined_metrics_repo):
        mock_combined_metrics_repo.list_metric_years = AsyncMock(return_value=[2026, 2025])

        response = client.get("/api/v1/report/years?namespace=default")

        assert response.status_code == 200
        assert response.json() == [2026, 2025]
        assert mock_combined_metrics_repo.list_metric_years.await_args is not None
        assert mock_combined_metrics_repo.list_metric_years.await_args.kwargs == {"namespace": "default"}


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
        assert reader.fieldnames is not None
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

    def test_export_invalid_last_returns_400(self, client):
        """An invalid time range must fail before the stream starts."""
        response = client.get("/api/v1/report/export?last=not-a-range")
        assert response.status_code == 400

    def test_export_custom_range_missing_end_returns_400(self, client):
        response = client.get("/api/v1/report/export?start=2026-01-01")
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

    def test_export_with_namespace_grouping(self, client, mock_combined_metrics_repo):
        """Namespace grouping should collapse pods into one row per namespace and period."""
        timestamp = datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc)
        metrics = [
            CombinedMetric(
                pod_name="api-a",
                namespace="default",
                timestamp=timestamp,
                duration_seconds=300,
                joules=100,
                co2e_grams=10,
                total_cost=1,
                embodied_co2e_grams=2,
            ),
            CombinedMetric(
                pod_name="api-b",
                namespace="default",
                timestamp=timestamp,
                duration_seconds=300,
                joules=200,
                co2e_grams=20,
                total_cost=2,
                embodied_co2e_grams=4,
            ),
        ]
        mock_combined_metrics_repo.read_combined_metrics_smart = AsyncMock(return_value=metrics)

        response = client.get("/api/v1/report/export?format=json&aggregate=true&granularity=daily&group_by=namespace")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["namespace"] == "default"
        assert data[0]["pod_name"] == ""
        assert data[0]["co2e_grams"] == 30
        assert data[0]["embodied_co2e_grams"] == 6

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
        """YTD exports should start from January 1st of the current UTC year."""
        mock_combined_metrics_repo.read_combined_metrics_smart = AsyncMock(return_value=[])

        response = client.get("/api/v1/report/export?format=json&last=ytd")

        assert response.status_code == 200
        # Export streams in 7-day chunks; check the first chunk starts on Jan 1.
        first_call = mock_combined_metrics_repo.read_combined_metrics_smart.await_args_list[0].kwargs
        start = first_call["start_time"]
        assert start.month == 1
        assert start.day == 1
        assert start.hour == 0
        assert start.minute == 0
        assert start.second == 0
        assert start.tzinfo is not None

    def test_export_invalid_group_by_returns_400(self, client):
        """Exporting with an unsupported group_by value must be rejected."""
        response = client.get("/api/v1/report/export?aggregate=true&group_by=node")
        assert response.status_code == 400


class TestReportInternalHelpers:
    """Tests for the internal _get_time_ranges helpers (exercised via the API)."""

    def test_summary_invalid_year_returns_400(self, client):
        """A year outside [1, 9999] must return 400."""
        response = client.get("/api/v1/report/summary?years=0")
        assert response.status_code == 400

    def test_summary_years_and_last_together_returns_400(self, client):
        """Mixing years= and last= must be rejected."""
        response = client.get("/api/v1/report/summary?years=2025&last=7d")
        assert response.status_code == 400

    def test_summary_start_and_last_together_returns_400(self, client):
        """Mixing start= and last= must be rejected."""
        response = client.get("/api/v1/report/summary?start=2025-01-01&last=7d")
        assert response.status_code == 400

    def test_summary_end_before_start_returns_400(self, client):
        """When end is before start the summary endpoint must return 400."""
        response = client.get("/api/v1/report/summary?start=2025-06-01&end=2025-01-01")
        assert response.status_code == 400


class TestCsvSanitization:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("=1+1", "'=1+1"),
            ("  =cmd", "'  =cmd"),
            ("\t@cmd", "'\t@cmd"),
            ("\r+cmd", "'\r+cmd"),
            ("-10", "'-10"),
            ("normal", "normal"),
            ("", ""),
        ],
    )
    def test_sanitize_cell_blocks_formula_injection(self, value, expected):
        from greenkube.api.routers.report import _sanitize_cell

        assert _sanitize_cell(value) == expected


IDLE = {"idle_co2e_grams": 3.0, "idle_embodied_co2e_grams": 0.5, "idle_cost": 0.01, "idle_energy_joules": 7000.0}


class TestReportSummaryIdleShare:
    """The yearly report shows the node idle share next to the allocated totals (NRG-02 acceptance e)."""

    @pytest.mark.parametrize("query", ["", "?aggregate=true&granularity=daily"])
    def test_cluster_wide_summary_adds_idle_and_cluster_totals(
        self, client, mock_combined_metrics_repo, sample_combined_metrics, query
    ):
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        mock_combined_metrics_repo.aggregate_idle_summary = AsyncMock(return_value=dict(IDLE))
        data = client.get(f"/api/v1/report/summary{query}").json()
        assert abs(data["total_energy_joules"] - 20000.0) < 0.01  # allocated to workloads, unchanged
        assert data["idle_energy_joules"] == pytest.approx(7000.0)
        assert data["idle_co2e_grams"] == pytest.approx(3.0)
        assert data["cluster_total_energy_joules"] == pytest.approx(27000.0)
        assert data["cluster_total_co2e_grams"] == pytest.approx(5.7 + 3.0, abs=0.01)
        assert data["cluster_total_embodied_co2e_grams"] == pytest.approx(0.17 + 0.5, abs=0.01)
        assert data["cluster_total_cost"] == pytest.approx(0.017 + 0.01, abs=0.001)

    def test_idle_share_is_summed_over_selected_years(self, client, mock_combined_metrics_repo):
        mock_combined_metrics_repo.aggregate_idle_summary = AsyncMock(return_value=dict(IDLE))
        data = client.get("/api/v1/report/summary?years=2024&years=2025").json()
        assert mock_combined_metrics_repo.aggregate_idle_summary.await_count == 2
        assert data["idle_energy_joules"] == pytest.approx(14000.0)

    def test_namespace_report_has_no_idle_share(self, client, mock_combined_metrics_repo, sample_combined_metrics):
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        mock_combined_metrics_repo.aggregate_idle_summary = AsyncMock(return_value=dict(IDLE))
        data = client.get("/api/v1/report/summary?namespace=default").json()
        assert data["idle_energy_joules"] is None
        assert data["cluster_total_energy_joules"] is None
        mock_combined_metrics_repo.aggregate_idle_summary.assert_not_awaited()

    def test_no_idle_rows_leave_the_idle_share_unknown(self, client):
        data = client.get("/api/v1/report/summary").json()
        assert data["idle_energy_joules"] is None
        assert data["cluster_total_cost"] is None

    def test_a_short_idle_history_leaves_the_idle_share_unknown(
        self, client, mock_combined_metrics_repo, sample_combined_metrics
    ):
        """Pod rows older than the first idle row: the idle share would cover part of the report only."""
        since = datetime.now(timezone.utc)
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        mock_combined_metrics_repo.aggregate_idle_summary = AsyncMock(return_value=dict(IDLE))
        mock_combined_metrics_repo.idle_data_since = AsyncMock(return_value=since)
        data = client.get("/api/v1/report/summary").json()
        assert data["idle_energy_joules"] is None
        assert data["cluster_total_energy_joules"] is None
        assert data["idle_data_since"] is not None
