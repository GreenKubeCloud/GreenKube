"""The realization context survives a controller restart (IMP-12).

Runs on SQLite and, with ``GREENKUBE_TEST_POSTGRES_DSN``, on PostgreSQL.
"""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.impact import ImpactProjector
from greenkube.impact.realization import (
    CLUSTER_POOL,
    SOURCE_CONTEXT,
    NodeObservation,
    RealizationInputs,
    current_realization_context,
    refresh_realization_context,
    register_realization_store,
    restore_realization_context,
    set_realization_context,
)
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo
from greenkube.storage.realization_repository import RealizationContextRepository
from tests.integration.test_change_measurement import database  # noqa: F401  # pyrefly: ignore[missing-import]


class _Collector:
    async def collect(self):
        return RealizationInputs(nodes=[NodeObservation("n1")])


@pytest.fixture(autouse=True)
def _clean_state():
    set_realization_context(None)
    register_realization_store(None)
    yield
    set_realization_context(None)
    register_realization_store(None)


async def test_a_restart_restores_the_fixed_node_context(database):  # noqa: F811
    store = RealizationContextRepository(database)
    register_realization_store(store)
    now = datetime.now(timezone.utc)
    context = await refresh_realization_context(Config(), collector=_Collector(), now=now, force=True)
    assert context is not None and context.pools[CLUSTER_POOL].factor == 0.0

    # Restart: the process cache is empty and Prometheus (hence the refresh) is down.
    set_realization_context(None)
    restored = await restore_realization_context(store, now=now + timedelta(hours=1))
    assert restored is not None and current_realization_context() is restored
    assert restored.pools[CLUSTER_POOL].factor == 0.0
    assert any("restored after a restart" in reason for reason in restored.pools[CLUSTER_POOL].reasons)

    row = CombinedMetric(
        pod_name="api-1",
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        node="n1",
        cpu_request=1000,
        cpu_usage_millicores=50,
        memory_request=1024**3,
        timestamp=now - timedelta(minutes=30),
        duration_seconds=300,
    )
    projector = ImpactProjector(Config(), node_infos=[NodeInfo(name="n1", cpu_capacity_cores=4)], rows=[row])
    delta = projector.container_resources(
        projector.workload_ref("prod", "Deployment", "api"), [row], cpu_millicores=(1000, 100)
    )
    assert delta.allocated_cost_annual > 0
    assert delta.expected_billable_cost_annual == 0.0
    assert delta.assumptions["realization_factor_source"] == SOURCE_CONTEXT


async def test_a_context_older_than_a_day_is_not_restored(database):  # noqa: F811
    store = RealizationContextRepository(database)
    register_realization_store(store)
    now = datetime.now(timezone.utc)
    await refresh_realization_context(Config(), collector=_Collector(), now=now, force=True)
    set_realization_context(None)
    assert await restore_realization_context(store, now=now + timedelta(hours=25)) is None
    assert current_realization_context() is None
