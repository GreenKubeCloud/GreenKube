"""COL-16 review: the usage rollup takes a pod's workload from its latest row with an owner.

Rows stored before the owner chain change say ``Deployment/web`` for a pod of
the Argo Rollout ``web``; rows written after say ``Rollout/web``. The pod's
target follows the newest owner, so the transition hour is not split.
"""

from datetime import datetime, timedelta, timezone

from greenkube.core.workload_usage import rollup_rows
from greenkube.models.metrics import CombinedMetric

HOUR = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)


def _row(minute: int, owner_kind):
    return CombinedMetric(
        pod_name="web-6d5f7c9b8-abcde",
        namespace="shop",
        owner_kind=owner_kind,
        owner_name="web" if owner_kind else None,
        timestamp=HOUR + timedelta(minutes=minute),
        duration_seconds=300,
        cpu_request=100,
        memory_request=0,
        cpu_usage_millicores=50,
    )


def test_the_latest_owner_names_the_target():
    rows = [_row(0, "Deployment"), _row(5, "Deployment"), _row(10, "Rollout"), _row(15, None)]

    targets = {(hour.target_kind, hour.target_name) for hour in rollup_rows(rows)}

    assert targets == {("Rollout", "web")}
