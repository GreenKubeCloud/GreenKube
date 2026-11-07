# tests/impact/test_pricing.py
"""Tests for the cost fallback chain and PricingCatalog (roadmap B3)."""

import json
from types import SimpleNamespace

import pytest

from greenkube.core.config import Config
from greenkube.impact.pricing import (
    CATALOG_PROVIDERS,
    PRICING_DATA_DIR,
    CustomRates,
    ObservedRates,
    PricingCatalog,
    UnitPriceResolver,
    apportion_node_price,
    observed_rates_from_rows,
    opencost_default_rates,
)
from greenkube.impact.types import CostSource

CATALOG = {
    "schema_version": 1,
    "provider": "aws",
    "currency": "USD",
    "columns": ["hourly", "vcpu", "memory_gib"],
    "regions": {"eu-west-3": {"m5.large": [0.112, 2, 8.0]}},
}


class TestOpenCostDefaults:
    def test_defaults_equal_opencost_published_custom_pricing(self):
        """Default rates are OpenCost's configs/default.json values, never invented."""
        rates = opencost_default_rates()
        assert rates.cpu_core_hour == 0.031611
        assert rates.ram_gib_hour == 0.004237
        assert rates.storage_gib_hour == 0.00005479452
        assert rates.currency == "USD"
        assert "opencost/opencost" in rates.source

    def test_default_rates_are_used_when_nothing_is_configured(self):
        prices = CustomRates.from_config(Config()).unit_prices()
        assert prices.cpu_core_hour == 0.031611
        assert prices.ram_gib_hour == 0.004237
        assert prices.source == CostSource.CUSTOM_RATE
        assert prices.detail == "opencost_default"

    def test_configured_rates_override_defaults(self):
        config = Config(
            IMPACT_PRICING_CPU_CORE_HOUR=0.05, IMPACT_PRICING_RAM_GIB_HOUR=0.01, IMPACT_PRICING_CURRENCY="eur"
        )
        prices = CustomRates.from_config(config).unit_prices()
        assert (prices.cpu_core_hour, prices.ram_gib_hour, prices.currency) == (0.05, 0.01, "EUR")
        assert prices.detail == "configured"


class TestApportionNodePrice:
    def test_matches_opencost_algorithm_and_preserves_node_price(self):
        cpu, ram = apportion_node_price(0.112, 2, 8)
        ratio = 0.031611 / 0.004237
        assert ram == pytest.approx(0.112 / (2 * ratio + 8))
        assert cpu == pytest.approx(ram * ratio)
        assert 2 * cpu + 8 * ram == pytest.approx(0.112)

    def test_zero_price_or_shape_yields_zero(self):
        assert apportion_node_price(0.0, 2, 8) == (0.0, 0.0)
        assert apportion_node_price(1.0, 0, 0) == (0.0, 0.0)


class TestPricingCatalog:
    def test_lookup_is_case_insensitive(self):
        catalog = PricingCatalog(documents={"aws": CATALOG})
        entry = catalog.lookup("AWS", "EU-WEST-3", "M5.Large")
        assert entry is not None
        assert (entry.hourly, entry.vcpus, entry.memory_gib) == (0.112, 2.0, 8.0)

    def test_unknown_provider_region_or_type_misses(self):
        catalog = PricingCatalog(documents={"aws": CATALOG})
        assert catalog.lookup("aws", "us-east-1", "m5.large") is None
        assert catalog.lookup("unknown", "eu-west-3", "m5.large") is None
        assert catalog.lookup("aws", None, "m5.large") is None

    def test_bundled_catalog_files_follow_the_schema(self):
        for provider in CATALOG_PROVIDERS:
            document = json.loads((PRICING_DATA_DIR / f"{provider}.json").read_text())
            assert document["schema_version"] == 1
            assert document["provider"] == provider
            assert document["columns"] == ["hourly", "vcpu", "memory_gib"]
            for region, instances in document["regions"].items():
                assert region == region.lower()
                for row in instances.values():
                    assert row[0] > 0
            if document["regions"]:
                assert document["source"], provider
                assert document["generated_at"], provider


class TestUnitPriceResolver:
    def _resolver(self, observed=None, config=None):
        return UnitPriceResolver(
            catalog=PricingCatalog(documents={"aws": CATALOG}),
            custom_rates=CustomRates.from_config(config or Config()),
            observed=observed,
        )

    def test_observed_per_resource_costs_win(self):
        observed = ObservedRates()
        observed.add(
            cost_cpu=0.2,
            cost_ram=0.05,
            cpu_reserved_cores=2,
            mem_reserved_gib=5,
            hours=1,
            source="opencost",
        )
        prices = self._resolver({"n1": observed}).resolve(
            node_name="n1", provider="aws", region="eu-west-3", instance_type="m5.large"
        )
        assert prices.source == CostSource.OPENCOST
        assert prices.detail == "observed"
        assert prices.cpu_core_hour == pytest.approx(0.1)
        assert prices.ram_gib_hour == pytest.approx(0.01)

    def test_catalog_is_second(self):
        prices = self._resolver().resolve(node_name="n1", provider="aws", region="eu-west-3", instance_type="m5.large")
        assert prices.source == CostSource.PRICING_CATALOG
        assert prices.detail == "aws/eu-west-3/m5.large"
        assert 2 * prices.cpu_core_hour + 8 * prices.ram_gib_hour == pytest.approx(0.112)

    def test_custom_rates_are_last(self):
        prices = self._resolver().resolve(node_name="n1", provider="aws", region="mars-1", instance_type="m5.large")
        assert prices.source == CostSource.CUSTOM_RATE

    def test_partial_or_negative_observed_costs_fall_through(self):
        observed = ObservedRates()
        observed.add(cost_cpu=-1.0, cost_ram=0.05, cpu_reserved_cores=2, mem_reserved_gib=5, hours=1, source=None)
        prices = self._resolver({"n1": observed}).resolve(
            node_name="n1", provider="aws", region="eu-west-3", instance_type="m5.large"
        )
        assert prices.source == CostSource.PRICING_CATALOG

    def test_observed_rates_ignore_rows_without_component_costs(self):
        rows = [
            SimpleNamespace(node="n1", cpu_request=1000, memory_request=2**30, cpu_usage_millicores=None,
                            memory_usage_bytes=None, duration_seconds=3600),
            SimpleNamespace(node="n2", cpu_request=1000, memory_request=2**30, cpu_usage_millicores=2000,
                            memory_usage_bytes=None, duration_seconds=3600, cost_cpu=0.2, cost_ram=0.01,
                            cost_source="kubecost"),
        ]  # fmt: skip
        observed = observed_rates_from_rows(rows, default_hours=1 / 12)
        assert set(observed) == {"n2"}
        prices = observed["n2"].unit_prices("USD")
        assert prices is not None
        # max(request, usage) = 2 cores for one hour.
        assert prices.cpu_core_hour == pytest.approx(0.1)
        assert prices.source == CostSource.KUBECOST


class TestImpactSettings:
    def test_defaults(self):
        config = Config()
        assert config.CARBON_PRICE_PER_TONNE == 100.0
        assert config.IMPACT_REALIZATION_FACTOR == 1.0
        assert config.IMPACT_PRICING_CPU_CORE_HOUR is None
        assert config.IMPACT_PRICING_CURRENCY == "USD"

    def test_blank_prices_mean_unset(self):
        config = Config(IMPACT_PRICING_CPU_CORE_HOUR="", IMPACT_PRICING_RAM_GIB_HOUR=" ")
        assert config.IMPACT_PRICING_CPU_CORE_HOUR is None
        assert config.IMPACT_PRICING_RAM_GIB_HOUR is None

    @pytest.mark.parametrize(
        "settings",
        [
            {"IMPACT_REALIZATION_FACTOR": 1.5},
            {"IMPACT_REALIZATION_FACTOR": -0.1},
            {"CARBON_PRICE_PER_TONNE": -1},
            {"IMPACT_PRICING_CPU_CORE_HOUR": -0.1},
            {"IMPACT_PRICING_CURRENCY": "dollars"},
        ],
    )
    def test_invalid_values_are_rejected(self, settings):
        with pytest.raises(ValueError):
            Config(**settings)
