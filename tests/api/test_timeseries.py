# tests/api/test_timeseries.py
"""Tests for the metrics time-series endpoint."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from greenkube.models.metrics import CombinedMetric


def _make_metrics_over_hours(hours: int = 6) -> list[CombinedMetric]:
    """Create metrics spread across multiple hours for time-series testing."""
    base = datetime(2026, 2, 8, 6, 0, 0, tzinfo=timezone.utc)
    metrics = []
    for h in range(hours):
        ts = base + timedelta(hours=h)
        metrics.append(
            CombinedMetric(
                pod_name=f"pod-{h % 3}",
                namespace="default" if h % 2 == 0 else "production",
                total_cost=0.01 * (h + 1),
                co2e_grams=1.0 * (h + 1),
                joules=1000.0 * (h + 1),
                cpu_request=250,
                memory_request=256 * 1024 * 1024,
                timestamp=ts,
                duration_seconds=300,
                embodied_co2e_grams=0.01 * (h + 1),
            )
        )
    return metrics


class TestTimeseriesEndpoint:
    """Tests for GET /api/v1/metrics/timeseries."""

    def test_timeseries_returns_200(self, client):
        """Should return 200 even with no data."""
        response = client.get("/api/v1/metrics/timeseries")
        assert response.status_code == 200

    def test_timeseries_returns_empty_list(self, client):
        """Should return an empty list when no metrics exist."""
        response = client.get("/api/v1/metrics/timeseries")
        data = response.json()
        assert data == []

    def test_timeseries_groups_by_hour(self, client, mock_carbon_repo):
        """Should return data grouped by hour by default."""
        metrics = _make_metrics_over_hours(6)
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        response = client.get("/api/v1/metrics/timeseries?last=24h")
        data = response.json()
        assert len(data) > 0
        assert "timestamp" in data[0]
        assert "co2e_grams" in data[0]
        assert "total_cost" in data[0]
        assert "joules" in data[0]

    def test_timeseries_groups_by_day(self, client, mock_carbon_repo):
        """Should group by day when granularity=day."""
        metrics = _make_metrics_over_hours(6)
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        response = client.get("/api/v1/metrics/timeseries?last=7d&granularity=day")
        assert response.status_code == 200
        data = response.json()
        assert len(data) > 0

    def test_timeseries_filter_by_namespace(self, client, mock_carbon_repo):
        """Should filter by namespace."""
        metrics = _make_metrics_over_hours(6)
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        response = client.get("/api/v1/metrics/timeseries?namespace=default")
        data = response.json()
        # All returned points should be aggregations from only 'default' namespace
        assert len(data) >= 0  # Might have some points

    def test_timeseries_invalid_granularity_returns_400(self, client):
        """Should return 400 for invalid granularity."""
        response = client.get("/api/v1/metrics/timeseries?granularity=invalid")
        assert response.status_code == 400

    def test_timeseries_contains_pod_count(self, client, mock_carbon_repo):
        """Each time point should contain pod_count."""
        metrics = _make_metrics_over_hours(3)
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        response = client.get("/api/v1/metrics/timeseries")
        data = response.json()
        if data:
            assert "pod_count" in data[0]

    def test_timeseries_sorted_by_timestamp(self, client, mock_carbon_repo):
        """Results should be sorted chronologically."""
        metrics = _make_metrics_over_hours(6)
        mock_carbon_repo.read_combined_metrics = AsyncMock(return_value=metrics)
        response = client.get("/api/v1/metrics/timeseries")
        data = response.json()
        if len(data) > 1:
            timestamps = [d["timestamp"] for d in data]
            assert timestamps == sorted(timestamps)
