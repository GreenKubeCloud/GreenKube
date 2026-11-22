# tests/impact/test_co2e_components.py
"""Projected CO2e split into components, and the component-matched comparison value."""

from __future__ import annotations

import pytest

from greenkube.core.config import Config
from greenkube.impact import ImpactProjector, impact_fields
from greenkube.impact.comparison import measurable_co2e_projection, projected_co2e_components
from greenkube.impact.types import (
    CO2E_EMBODIED,
    CO2E_IDLE_SHARE,
    CO2E_OPERATIONAL_ENERGY,
    MEASURED_CO2E_COMPONENTS,
    Co2eBreakdown,
    co2e_components,
)
from tests.impact.test_coverage_annualization import LOOKBACK, _now, _rows  # pyrefly: ignore[missing-import]


def test_components_partition_the_breakdown():
    breakdown = Co2eBreakdown(
        idle_operational=1.0, dynamic_operational=2.0, embodied=3.0, memory_operational=4.0, gpu_operational=5.0
    )
    split = co2e_components(breakdown)
    assert split == {CO2E_OPERATIONAL_ENERGY: 11.0, CO2E_IDLE_SHARE: 1.0, CO2E_EMBODIED: 3.0}
    assert sum(split.values()) == pytest.approx(breakdown.total)
    assert CO2E_EMBODIED not in MEASURED_CO2E_COMPONENTS


def test_projection_reports_components_and_the_measurable_value():
    now = _now()
    rows = _rows(now, 48.0)
    projector = ImpactProjector(Config(), rows=rows, analysis_window_seconds=LOOKBACK, window_end=now)
    ref = projector.workload_ref("apps", "Deployment", "young")
    delta = projector.container_resources(ref, rows, cpu_millicores=(500, 200), memory_bytes=(1024**3, 512 * 1024**2))
    impact = impact_fields(delta)["impact"]

    components = impact["co2e_components_annual_g"]
    breakdown = impact["co2e_breakdown_annual_g"]
    assert sum(components.values()) == pytest.approx(breakdown["total"])
    assert components[CO2E_EMBODIED] > 0 and components[CO2E_IDLE_SHARE] > 0
    measurable = (breakdown["total"] - breakdown["embodied"]) * impact["realization_factor"]
    assert impact["co2e_measurable_expected_annual_g"] == pytest.approx(measurable)
    assert impact["co2e_measurable_components"] == list(MEASURED_CO2E_COMPONENTS)
    # A measured value is compared with this, never with the total that includes embodied.
    assert measurable_co2e_projection(impact) == pytest.approx(measurable)
    assert measurable_co2e_projection(impact) < impact["co2e_expected_annual_g"]


def test_measurable_projection_of_older_and_partial_projections():
    legacy = {
        "co2e_modeled": True,
        "realization_factor": 0.5,
        "co2e_breakdown_annual_g": {"idle_operational": 10.0, "embodied": 30.0, "total": 40.0},
    }
    assert projected_co2e_components(legacy) == {
        CO2E_OPERATIONAL_ENERGY: 0.0,
        CO2E_IDLE_SHARE: 10.0,
        CO2E_EMBODIED: 30.0,
    }
    assert measurable_co2e_projection(legacy) == pytest.approx(5.0)
    assert measurable_co2e_projection({**legacy, "realization_factor": 7.0}) == pytest.approx(10.0)
    assert measurable_co2e_projection(None) is None
    assert measurable_co2e_projection({}) is None
    assert measurable_co2e_projection({"co2e_modeled": False, "co2e_breakdown_annual_g": {"embodied": 1.0}}) is None
    assert measurable_co2e_projection({"co2e_breakdown_annual_g": {"idle_operational": "x"}}) is None
