from greenkube.core.cost_normalizer import CostNormalizer
from greenkube.models.metrics import CostMetric


def test_cost_normalizer_divides_known_costs_by_step_count():
    cost_map = {"api": CostMetric(pod_name="api", namespace="prod", cpu_cost=0.5, ram_cost=1.5, total_cost=2.0)}

    assert CostNormalizer.per_step_cost(cost_map, "api", steps_per_day=4) == 0.5
    assert CostNormalizer.per_range_cost(cost_map, "api", steps_in_range=8) == 0.25


def test_cost_normalizer_uses_default_when_pod_cost_is_missing():
    assert CostNormalizer.per_step_cost({}, "missing", steps_per_day=4) == 0.0
    assert CostNormalizer.per_range_cost({}, "missing", steps_in_range=8) == 0.0
