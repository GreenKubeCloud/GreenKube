# tests/models/test_recommendation_status.py
"""BUG-24: recommendation statuses are lifecycle states, never plan event names."""

import pytest

from greenkube.models.metrics import RecommendationStatus


@pytest.mark.parametrize("event_type", ["plan_created", "plan_status_changed", "plan_action_status_changed"])
def test_plan_event_types_are_not_recommendation_statuses(event_type):
    with pytest.raises(ValueError):
        RecommendationStatus(event_type)
