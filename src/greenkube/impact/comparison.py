# src/greenkube/impact/comparison.py
"""Component-matched comparison of projected and measured impact.

Estimation and measurement use the same Impact Model: the rows a change
measurement reads were allocated by it, and the projection it is compared
with was computed by it. They must also cover the same **components**. A
projection splits its CO2e into operational energy, idle share and embodied
emissions (:func:`~greenkube.impact.types.co2e_components`); a measurement
over stored metric rows sees only :data:`MEASURED_CO2E_COMPONENTS`. A measured
value is therefore compared with the projected value of those components only,
never with a total that includes components the measurement cannot see.

They must also be on the same **basis**: a measurement over stored rows sees
*allocated* cost and CO2e (projected != allocated != realized), so its
projection accuracy is measured / allocated projection, never measured /
realization-scaled (billable) projection (``allocated=True``).
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from greenkube.impact.types import (
    CO2E_COMPONENTS,
    MEASURED_CO2E_COMPONENTS,
    Co2eBreakdown,
    co2e_components,
)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def projected_co2e_components(impact: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Allocated annual CO2e by component from a persisted ``impact`` projection.

    Works for every model version: the components are derived from
    ``co2e_breakdown_annual_g`` when the projection predates them. None when the
    projection is missing, does not model CO2e or has no breakdown.
    """
    if not isinstance(impact, Mapping) or impact.get("co2e_modeled") is False:
        return None
    stored = impact.get("co2e_components_annual_g")
    if isinstance(stored, Mapping):
        values = {name: _number(stored.get(name)) for name in CO2E_COMPONENTS}
        if all(value is not None for value in values.values()):
            return {name: float(value or 0.0) for name, value in values.items()}
    breakdown = impact.get("co2e_breakdown_annual_g")
    if not isinstance(breakdown, Mapping):
        return None
    fields = ("idle_operational", "dynamic_operational", "memory_operational", "gpu_operational", "embodied")
    values = {name: _number(breakdown.get(name)) for name in fields}
    if all(value is None for value in values.values()):
        return None
    return co2e_components(Co2eBreakdown(**{name: value or 0.0 for name, value in values.items()}))


def measurable_co2e_projection(
    impact: Optional[Mapping[str, Any]],
    components: Sequence[str] = MEASURED_CO2E_COMPONENTS,
    *,
    allocated: bool = False,
) -> Optional[float]:
    """Projected annual CO2e of ``components`` only.

    By default it is the allocated value of those components times the
    projection's realization factor, the same basis as
    ``co2e_expected_annual_g``. With ``allocated`` it is the allocated value
    alone: what a change measurement (which reads rows allocated by the Impact
    Model, before any realization) is compared with. None (nothing to compare
    with: fail closed) when the projection is missing or does not model CO2e.
    """
    split = projected_co2e_components(impact)
    if split is None or impact is None:
        return None
    if allocated:
        return sum(split.get(name, 0.0) for name in components)
    realization = _number(impact.get("realization_factor"))
    realization = 1.0 if realization is None else min(max(realization, 0.0), 1.0)
    return sum(split.get(name, 0.0) for name in components) * realization


__all__ = ["measurable_co2e_projection", "projected_co2e_components"]
