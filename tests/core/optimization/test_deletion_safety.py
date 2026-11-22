# tests/core/optimization/test_deletion_safety.py
"""Safety gates of destructive recommendations (orphaned LoadBalancer and PersistentVolume deletions).

A deletion is recommended only with evidence that the resource is unused, for
at least DELETION_MIN_UNUSED_DAYS, and never for resources that run the
cluster or are opted out. Timestamps are derived from
the current time (never fixed dates against the wall clock).
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import AsyncMock

import pytest

from greenkube.collectors.deletion_candidates import (
    LOAD_BALANCER_KIND,
    PERSISTENT_VOLUME_KIND,
    attach_first_seen,
)
from greenkube.collectors.lb_collector import OrphanedLoadBalancer
from greenkube.collectors.pv_collector import OrphanedPV
from greenkube.core.config import Config
from greenkube.core.deletion_safety import is_infrastructure_namespace, opt_out_reason
from greenkube.models.metrics import CombinedMetric, RecommendationType

from .helpers import NativeRecommender  # pyrefly: ignore[missing-import]

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _config(days: Optional[float] = None) -> Config:
    cfg = Config()
    cfg.RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS = 0
    if days is not None:
        cfg.DELETION_MIN_UNUSED_DAYS = days
    return cfg


def _rows(
    namespace: str,
    name: str,
    kind: str = "Deployment",
    days: float = 8.0,
    joules: float = 10.0,
    cost: float = 0.05,
) -> List[CombinedMetric]:
    """Two hourly samples ``days`` apart: an idle workload observed over that span."""
    return [
        CombinedMetric(
            pod_name=f"{name}-0",
            namespace=namespace,
            owner_kind=kind,
            owner_name=name,
            cpu_request=100,
            memory_request=128 * 1024**2,
            cpu_usage_millicores=0,
            memory_usage_bytes=8 * 1024**2,
            joules=joules,
            total_cost=cost,
            co2e_grams=0.01,
            timestamp=at,
            duration_seconds=3600,
            node="node-1",
            grid_intensity=50.0,
            emaps_zone="FR",
        )
        for at in (NOW - timedelta(days=days), NOW)
    ]


def _types(recs, rec_type):
    return [r for r in recs if r.type == rec_type]


# --------------------------------------------------------------------------- #
# Shared rules                                                                 #
# --------------------------------------------------------------------------- #


class TestRules:
    @pytest.mark.parametrize(
        "namespace",
        [
            "kube-system",
            "kube-flannel",
            "monitoring",
            "cert-manager",
            "argocd",
            "flux-system",
            "metallb-system",
            "longhorn-system",
            "istio-ingress",
            "openshift-monitoring",
            "prometheus-operator",
            "greenkube",
        ],
    )
    def test_infrastructure_namespaces(self, namespace):
        assert is_infrastructure_namespace(namespace)

    @pytest.mark.parametrize("namespace", ["default", "shop", "legacy", "team-a", "systems-lab"])
    def test_application_namespaces(self, namespace):
        assert not is_infrastructure_namespace(namespace)

    @pytest.mark.parametrize(
        "annotations,expected",
        [
            ({"greenkube.io/ignore": "true"}, True),
            ({"greenkube.io/ignore": "True"}, True),
            ({"greenkube.io/ignore": "false"}, False),
            ({"greenkube.io/keep": ""}, True),
            ({"greenkube.io/keep": "audit until 2027"}, True),
            ({"greenkube.io/keep": "false"}, False),
            ({}, False),
        ],
    )
    def test_opt_out_annotations(self, annotations, expected):
        assert (opt_out_reason({"annotations": annotations}) is not None) is expected


# --------------------------------------------------------------------------- #
# Orphaned LoadBalancer / PersistentVolume analyzers                           #
# --------------------------------------------------------------------------- #


def _lb(first_seen: Optional[datetime], evidence=("selector app=old matches no pod",)) -> OrphanedLoadBalancer:
    return OrphanedLoadBalancer(
        name="old-api",
        namespace="legacy",
        external_ip="203.0.113.10",
        evidence=list(evidence),
        first_seen_at=first_seen,
    )


def _pv(first_seen: Optional[datetime], evidence=("phase Released",)) -> OrphanedPV:
    return OrphanedPV(
        name="pvc-1", phase="Released", capacity_bytes=10 * 1024**3, evidence=list(evidence), first_seen_at=first_seen
    )


class TestAssetDeletionAge:
    @pytest.mark.parametrize(
        "unused_for,days,expected",
        [
            (timedelta(days=8), None, 1),  # default 7 days
            (timedelta(days=2), None, 0),  # not long enough
            (None, None, 0),  # duration not proven (no database)
            (timedelta(days=8), 14.0, 0),  # configurable
            (timedelta(hours=1), 0.0, 1),
        ],
    )
    def test_load_balancer_needs_a_proven_unused_period(self, unused_for, days, expected):
        first_seen = NOW - unused_for if unused_for is not None else None
        recs = NativeRecommender(_config(days)).generate_recommendations(
            _rows("legacy", "web", joules=1e9), load_balancers=[_lb(first_seen)]
        )
        lbs = _types(recs, RecommendationType.ORPHANED_LOAD_BALANCER)
        assert len(lbs) == expected
        if lbs:
            assert "appears unused for" in lbs[0].description
            assert "unused continuously since" in lbs[0].reason

    def test_load_balancer_without_evidence_is_never_recommended(self):
        recs = NativeRecommender(_config()).generate_recommendations(
            _rows("legacy", "web", joules=1e9), load_balancers=[_lb(NOW - timedelta(days=30), evidence=())]
        )
        assert _types(recs, RecommendationType.ORPHANED_LOAD_BALANCER) == []

    @pytest.mark.parametrize("unused_for,expected", [(timedelta(days=8), 1), (timedelta(days=1), 0), (None, 0)])
    def test_persistent_volume_needs_a_proven_unused_period(self, unused_for, expected):
        first_seen = NOW - unused_for if unused_for is not None else None
        recs = NativeRecommender(_config()).generate_recommendations(
            _rows("legacy", "web", joules=1e9), persistent_volumes=[_pv(first_seen)]
        )
        pvs = _types(recs, RecommendationType.ORPHANED_PERSISTENT_VOLUME)
        assert len(pvs) == expected
        if pvs:
            assert "cannot be undone" in pvs[0].description


# --------------------------------------------------------------------------- #
# First-seen attachment                                                        #
# --------------------------------------------------------------------------- #


class TestAttachFirstSeen:
    @pytest.mark.asyncio
    async def test_incomplete_evaluation_records_nothing(self):
        repo = AsyncMock()
        assert await attach_first_seen(repo, LOAD_BALANCER_KIND, None, NOW) is None
        repo.observe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_without_repository_candidates_have_no_first_seen(self):
        items = await attach_first_seen(None, LOAD_BALANCER_KIND, [_lb(None)], NOW)
        assert items is not None and items[0].first_seen_at is None

    @pytest.mark.asyncio
    async def test_first_seen_comes_from_the_repository(self):
        since = NOW - timedelta(days=9)
        repo = AsyncMock()
        repo.observe = AsyncMock(return_value={("legacy", "old-api"): since})
        items = await attach_first_seen(repo, LOAD_BALANCER_KIND, [_lb(None)], NOW)
        assert items is not None and items[0].first_seen_at == since
        assert repo.observe.await_args is not None
        kind, observations = repo.observe.await_args.args
        assert kind == LOAD_BALANCER_KIND
        assert [(o.namespace, o.name, o.evidence) for o in observations] == [
            ("legacy", "old-api", ["selector app=old matches no pod"])
        ]

    @pytest.mark.asyncio
    async def test_persistent_volumes_are_cluster_scoped(self):
        repo = AsyncMock()
        repo.observe = AsyncMock(return_value={("", "pvc-1"): NOW})
        items = await attach_first_seen(repo, PERSISTENT_VOLUME_KIND, [_pv(None)], NOW)
        assert items is not None and items[0].first_seen_at == NOW

    @pytest.mark.asyncio
    async def test_repository_failure_fails_closed(self):
        repo = AsyncMock()
        repo.observe = AsyncMock(side_effect=RuntimeError("db down"))
        items = await attach_first_seen(repo, LOAD_BALANCER_KIND, [_lb(None)], NOW)
        assert items is not None and items[0].first_seen_at is None


@pytest.fixture(autouse=True)
def _wall_clock_at_test_time(monkeypatch):
    """Re-read ``NOW`` for each test: a wall-clock constant taken at import ages during a long suite."""
    monkeypatch.setitem(globals(), "NOW", datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0))
