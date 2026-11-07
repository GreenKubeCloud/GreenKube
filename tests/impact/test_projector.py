# tests/impact/test_projector.py
"""Tests for the bridge between stored metrics and the Impact Model."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.impact import CostSource, ImpactProjector, PowerSource, PricingCatalog
from greenkube.impact.projector import UNKNOWN_NODE, impact_fields, observed_window_hours, sample_from_metric
from greenkube.impact.types import HOURS_PER_YEAR, WorkloadRef
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo

GIB = 1024**3
START = datetime(2026, 11, 1, tzinfo=timezone.utc)
EMPTY_CATALOG = PricingCatalog(documents={provider: {} for provider in ("aws", "gcp", "azure", "ovh", "scaleway")})


def _row(**overrides) -> CombinedMetric:
    values = dict(
        pod_name="api-1",
        namespace="prod",
        node="n1",
        cpu_request=500,
        cpu_usage_millicores=100,
        memory_request=GIB,
        memory_usage_bytes=GIB // 2,
        total_cost=0.01,
        pue=1.2,
        grid_intensity=250.0,
        timestamp=START,
        duration_seconds=300,
    )
    values.update(overrides)
    return CombinedMetric(**values)


class TestSamples:
    def test_sample_converts_units(self):
        ref = WorkloadRef("c", "prod", "Pod", "api-1")
        sample = sample_from_metric(_row(), ref, default_hours=1 / 12)
        assert sample.hours == pytest.approx(300 / 3600)
        assert sample.cpu_request_cores == 0.5
        assert sample.cpu_usage_cores == 0.1
        assert sample.mem_request_gib == 1.0
        assert sample.mem_usage_gib == 0.5
        assert sample.observed_cost_total == 0.01
        assert sample.observed_cost_cpu is None  # CombinedMetric has no component costs yet

    def test_missing_duration_uses_step_times_sample_count(self):
        ref = WorkloadRef("c", "prod", "Pod", "api-1")
        sample = sample_from_metric(_row(duration_seconds=None, sample_count=12), ref, default_hours=1 / 12)
        assert sample.hours == pytest.approx(1.0)

    def test_non_positive_costs_are_not_observed(self):
        ref = WorkloadRef("c", "prod", "Pod", "api-1")
        assert sample_from_metric(_row(total_cost=-1.0), ref, 1 / 12).observed_cost_total is None

    def test_window_falls_back_to_observed_span(self):
        ref = WorkloadRef("c", "prod", "Pod", "api-1")
        rows = [_row(timestamp=START + timedelta(hours=index)) for index in range(3)]
        samples = [sample_from_metric(row, ref, 1 / 12) for row in rows]
        assert observed_window_hours(samples) == pytest.approx(2 + 300 / 3600)


class TestNodeProfiles:
    def test_known_instance_type_uses_instance_power_profile(self):
        from greenkube.data.instance_profiles import INSTANCE_PROFILES

        node = NodeInfo(name="n1", instance_type="m5.large", cloud_provider="aws", cpu_capacity_cores=2,
                        memory_capacity_bytes=8 * GIB, embodied_emissions_kg=120.0)  # fmt: skip
        projector = ImpactProjector(Config(), node_infos=[node], rows=[_row()], catalog=EMPTY_CATALOG)
        profile = projector.pool.nodes["n1"]
        assert profile.power_source == PowerSource.INSTANCE_PROFILE
        assert profile.idle_watts == INSTANCE_PROFILES["m5.large"]["minWatts"]
        assert profile.embodied_g_per_hour == pytest.approx(120_000 / (4 * HOURS_PER_YEAR))
        # Observed environment of the node's rows.
        assert (profile.pue, profile.grid_intensity_g_per_kwh) == (1.2, 250.0)

    def test_unknown_instance_type_derives_power_from_capacity(self):
        config = Config()
        node = NodeInfo(name="n1", instance_type="custom-8", cpu_capacity_cores=8, memory_capacity_bytes=32 * GIB)
        profile = ImpactProjector(config, node_infos=[node], catalog=EMPTY_CATALOG).pool.nodes["n1"]
        assert profile.power_source == PowerSource.NODE_CAPACITY_DERIVED
        per_core = config.DEFAULT_INSTANCE_MIN_WATTS / config.DEFAULT_INSTANCE_VCORES
        assert profile.idle_watts == pytest.approx(per_core * 8)
        assert profile.embodied_g_per_hour == pytest.approx(
            config.DEFAULT_EMBODIED_EMISSIONS_KG * 1000 / (config.DEFAULT_HARDWARE_LIFESPAN_YEARS * HOURS_PER_YEAR)
        )

    def test_unknown_nodes_use_a_flagged_default_profile(self):
        projector = ImpactProjector(Config(), catalog=EMPTY_CATALOG)
        profile = projector.pool.node_for("never-seen")
        assert profile.name == UNKNOWN_NODE
        assert profile.is_fallback
        assert profile.power_source == PowerSource.DEFAULT_PROFILE
        assert profile.prices.source == CostSource.CUSTOM_RATE
        assert profile.grid_intensity_g_per_kwh == Config().DEFAULT_INTENSITY

    def test_nodes_seen_only_in_rows_keep_their_instance_type(self):
        projector = ImpactProjector(
            Config(), rows=[_row(node="ghost", node_instance_type="m5.large")], catalog=EMPTY_CATALOG
        )
        assert projector.pool.nodes["ghost"].power_source == PowerSource.INSTANCE_PROFILE

    def test_catalog_prices_need_provider_region_and_instance_type(self):
        catalog = PricingCatalog(
            documents={
                "aws": {
                    "schema_version": 1,
                    "currency": "USD",
                    "columns": ["hourly", "vcpu", "memory_gib"],
                    "regions": {"eu-west-3": {"m5.large": [0.112, 2, 8]}},
                }
            }
        )
        node = NodeInfo(name="n1", instance_type="m5.large", region="eu-west-3", cloud_provider="aws")
        profile = ImpactProjector(Config(), node_infos=[node], catalog=catalog).pool.nodes["n1"]
        assert profile.prices.source == CostSource.PRICING_CATALOG
        # Memory size comes from the catalog when the node snapshot lacks it.
        assert profile.mem_capacity_gib == 8
        assert profile.hourly_price == pytest.approx(0.112)


class TestProjectorFacade:
    def test_projector_is_cached_per_context(self):
        from greenkube.core.optimization.context import OptimizationContext

        context = OptimizationContext(config=Config(), metrics=[_row()], analysis_window_seconds=3600)
        assert ImpactProjector.for_context(context) is ImpactProjector.for_context(context)

    def test_settings_flow_into_the_pool(self):
        config = Config(IMPACT_REALIZATION_FACTOR=0.4, CARBON_PRICE_PER_TONNE=50, IMPACT_PRICING_CURRENCY="EUR")
        pool = ImpactProjector(config, catalog=EMPTY_CATALOG).pool
        assert pool.realization_factor == 0.4
        assert pool.carbon_price_per_tonne == 50
        assert pool.currency == "EUR"
        assert pool.realization_source == "config:IMPACT_REALIZATION_FACTOR"

    def test_impact_fields_keep_api_v1_savings_fields(self):
        projector = ImpactProjector(Config(), rows=[_row()], analysis_window_seconds=3600, catalog=EMPTY_CATALOG)
        delta = projector.container_resources(
            projector.workload_ref("prod", "Pod", "api-1"), [_row()], cpu_millicores=(500, 200)
        )
        fields = impact_fields(delta)
        assert fields["potential_savings_cost"] == delta.expected_billable_cost_annual
        assert fields["potential_savings_co2e_grams"] == delta.co2e_expected_annual_g
        assert fields["impact"]["capacity_released"]["cpu_core_hours"] == pytest.approx(
            0.3 * (300 / 3600) * HOURS_PER_YEAR
        )

    def test_asset_deletion_has_no_modeled_co2e(self):
        projector = ImpactProjector(Config(), catalog=EMPTY_CATALOG)
        delta = projector.asset_deletion(
            asset_kind="persistent_volume",
            allocated_cost_annual=12.0,
            component="storage",
            cost_source=CostSource.CUSTOM_RATE,
            detail="STORAGE_COST_PER_GIB_MONTH",
        )
        fields = impact_fields(delta)
        assert fields["potential_savings_cost"] == 12.0
        assert fields["potential_savings_co2e_grams"] is None
        assert delta.cost_breakdown_annual.storage == 12.0
        assert delta.realization_factor == 1.0


class TestEvidenceSavingsMethod:
    def test_evidence_names_the_impact_model_method(self):
        """The recommendations page shows the method that produced the savings, not the retired ratio."""
        from greenkube.core.optimization.analyzers import CpuRightsizingAnalyzer
        from greenkube.core.optimization.context import OptimizationContext
        from greenkube.core.optimization.evidence import build_evidence

        rows = [_row(timestamp=START + timedelta(minutes=5 * index)) for index in range(12)]
        context = OptimizationContext(config=Config(), metrics=rows, analysis_window_seconds=3600)
        (rec,) = CpuRightsizingAnalyzer(Config()).analyze(context)

        evidence = build_evidence(rec, context)

        assert evidence is not None
        assert evidence.savings_method == "impact_model/1.0.0/container_resources"
