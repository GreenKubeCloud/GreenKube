# tests/api/test_config_version.py
"""Tests for the config and version endpoints."""


class TestVersionEndpoint:
    """Tests for GET /api/v1/version."""

    def test_version_returns_200(self, client):
        """Should return 200."""
        response = client.get("/api/v1/version")
        assert response.status_code == 200

    def test_version_returns_version_string(self, client):
        """Should return a version string."""
        response = client.get("/api/v1/version")
        data = response.json()
        assert "version" in data
        assert isinstance(data["version"], str)
        assert len(data["version"]) > 0


class TestConfigEndpoint:
    """Tests for GET /api/v1/config."""

    def test_config_returns_200(self, client):
        """Should return 200."""
        response = client.get("/api/v1/config")
        assert response.status_code == 200

    def test_config_contains_expected_fields(self, client):
        """Should expose non-sensitive configuration."""
        response = client.get("/api/v1/config")
        data = response.json()
        expected_fields = [
            "db_type",
            "cloud_provider",
            "default_zone",
            "default_intensity",
            "default_pue",
            "log_level",
            "log_format",
            "normalization_granularity",
            "prometheus_query_range_step",
        ]
        for field in expected_fields:
            assert field in data, f"Missing config field: {field}"

    def test_config_does_not_expose_secrets(self, client):
        """Should NOT expose any secret/token values."""
        response = client.get("/api/v1/config")
        data = response.json()
        raw = str(data).lower()
        assert "token" not in raw
        assert "password" not in raw
        assert "secret" not in raw
        assert "bearer" not in raw

    def test_config_exposes_the_pricing_currency(self, client, monkeypatch):
        """The UI formats every amount in the pricing currency (no hard-coded '$')."""
        from greenkube.core.config import get_config

        monkeypatch.setattr(get_config(), "IMPACT_PRICING_CURRENCY", "EUR", raising=False)

        data = client.get("/api/v1/config").json()

        assert data["currency"] == "EUR"

    def test_config_exposes_the_vintage_of_the_bundled_intensity_defaults(self, client):
        """Rows computed with defaults use annual means of this year (NRG-04)."""
        from greenkube.data.electricity_maps_regions_grid_intensity_default import DEFAULT_INTENSITY_YEAR

        data = client.get("/api/v1/config").json()

        assert data["intensity_defaults_year"] == DEFAULT_INTENSITY_YEAR == 2024
