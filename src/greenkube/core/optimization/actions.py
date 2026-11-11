"""Build typed GitOps actions from recommendations through the registered action kinds (H0a)."""

from __future__ import annotations

from typing import Sequence

from greenkube.automation.actions import ACTION_KINDS
from greenkube.gitops.contracts import ActionPlanV3
from greenkube.models.metrics import Recommendation


def build_action_plans(recommendations: Sequence[Recommendation]) -> dict[int, ActionPlanV3]:
    """Each expressible recommendation id -> the action of the first registered kind that expresses it.

    Built-in kinds: ``container_resources`` groups rightsizing recommendations into one
    workload change set with an explicit operation per container.
    """
    return ACTION_KINDS.build_action_plans(recommendations)
