# tests/core/optimization/test_deletion_safety.py
"""Safety gates of destructive recommendations (deletions and scale-to-zero).

A deletion is recommended only with evidence that the resource is unused, for
at least DELETION_MIN_UNUSED_DAYS, and never for resources that run the
cluster, are idle by design, or are opted out. Timestamps are derived from
the current time (never fixed dates against the wall clock).
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import AsyncMock

import pytest

from greenkube.collectors.deletion_candidates import (
    LOAD_BALANCER_KIND,
    PERSISTENT_VOLUME_KIND,
    NamespaceInfo,
    attach_first_seen,
)
from greenkube.collectors.lb_collector import OrphanedLoadBalancer
from greenkube.collectors.pv_collector import OrphanedPV
from greenkube.core.config import Config
from greenkube.core.deletion_safety import is_infrastructure_namespace, looks_event_driven, opt_out_reason
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


def _namespaces(*names: str, age_days: float = 90.0, **overrides) -> dict:
    created = NOW - timedelta(days=age_days)
    return {name: NamespaceInfo(name=name, created_at=created, **overrides) for name in names}


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

    @pytest.mark.parametrize("name,expected", [("cert-manager-webhook", True), ("my-operator", True), ("web", False)])
    def test_event_driven_names(self, name, expected):
        assert looks_event_driven(name) is expected


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
# Zombie workloads                                                             #
# --------------------------------------------------------------------------- #


class TestZombieSafety:
    def test_idle_workload_observed_long_enough_is_reported_with_evidence(self):
        recs = NativeRecommender(_config()).generate_recommendations(_rows("legacy", "old-report"))
        zombies = _types(recs, RecommendationType.ZOMBIE_POD)
        assert [z.pod_name for z in zombies] == ["old-report"]
        assert "appears idle" in zombies[0].description
        assert "observed over 8.0 days" in zombies[0].reason

    def test_short_observation_is_not_enough(self):
        recs = NativeRecommender(_config()).generate_recommendations(_rows("legacy", "old-report", days=2))
        assert _types(recs, RecommendationType.ZOMBIE_POD) == []

    @pytest.mark.parametrize(
        "namespace,name,kind",
        [
            ("monitoring", "blackbox-exporter", "Deployment"),
            ("kube-system", "coredns", "Deployment"),
            ("legacy", "node-agent", "DaemonSet"),
            ("legacy", "billing-operator", "Deployment"),
            ("legacy", "admission-webhook", "Deployment"),
        ],
    )
    def test_idle_by_design_workloads_are_never_zombies(self, namespace, name, kind):
        recs = NativeRecommender(_config()).generate_recommendations(_rows(namespace, name, kind=kind))
        assert _types(recs, RecommendationType.ZOMBIE_POD) == []

    def test_opted_out_namespace(self):
        info = _namespaces("legacy", annotations={"greenkube.io/ignore": "true"})
        recs = NativeRecommender(_config()).generate_recommendations(_rows("legacy", "old-report"), namespace_info=info)
        assert _types(recs, RecommendationType.ZOMBIE_POD) == []


# --------------------------------------------------------------------------- #
# Idle namespaces                                                              #
# --------------------------------------------------------------------------- #


class TestIdleNamespaceSafety:
    def _idle(self, namespace_info, namespace="legacy", name="old-report", days=8.0):
        recs = NativeRecommender(_config()).generate_recommendations(
            _rows(namespace, name, days=days), namespace_info=namespace_info
        )
        return _types(recs, RecommendationType.IDLE_NAMESPACE)

    def test_idle_namespace_with_evidence(self):
        recs = self._idle(_namespaces("legacy"))
        assert [r.namespace for r in recs] == ["legacy"]
        assert "custom resources, Secrets or external consumers" in recs[0].description

    def test_gitops_managed_namespace_says_to_delete_from_git(self):
        info = _namespaces("legacy", labels={"argocd.argoproj.io/instance": "legacy-app"})
        recs = self._idle(info)
        assert len(recs) == 1
        assert "remove it from Git" in recs[0].description
        assert "Argo CD" in recs[0].reason

    @pytest.mark.parametrize(
        "info",
        [
            None,  # namespaces unreadable
            {},  # namespace no longer exists
            _namespaces("legacy", age_days=3),  # younger than the minimum period
            _namespaces("legacy", annotations={"greenkube.io/keep": ""}),
            _namespaces("legacy", phase="Terminating"),
        ],
    )
    def test_unproven_or_protected_namespaces(self, info):
        assert self._idle(info) == []

    def test_short_observation_is_not_enough(self):
        assert self._idle(_namespaces("legacy"), days=2) == []

    def test_namespace_hosting_an_operator_is_not_idle(self):
        assert self._idle(_namespaces("legacy"), name="db-operator") == []

    @pytest.mark.parametrize("namespace", ["default", "observability", "team-system"])
    def test_infrastructure_and_undeletable_namespaces(self, namespace):
        assert self._idle(_namespaces(namespace), namespace=namespace) == []


# --------------------------------------------------------------------------- #
# Off-peak scale to zero                                                       #
# --------------------------------------------------------------------------- #


def _business_hours(namespace: str, name: str, kind: str) -> List[CombinedMetric]:
    start = NOW.replace(hour=0) - timedelta(days=1)
    rows = []
    for hour in range(24):
        rows.append(
            CombinedMetric(
                pod_name=f"{name}-0",
                namespace=namespace,
                owner_kind=kind,
                owner_name=name,
                cpu_request=1000,
                cpu_usage_millicores=800 if 9 <= hour < 17 else 5,
                memory_request=256 * 1024**2,
                memory_usage_bytes=128 * 1024**2,
                joules=5000.0,
                total_cost=0.01,
                co2e_grams=1.0,
                timestamp=start + timedelta(hours=hour),
                duration_seconds=3600,
                node="node-1",
                grid_intensity=50.0,
                emaps_zone="FR",
            )
        )
    return rows


class TestOffPeakSafety:
    def _offpeak(self, namespace="shop", name="web", kind="Deployment", hpa=None, namespace_info=None):
        recs = NativeRecommender(_config()).generate_recommendations(
            _business_hours(namespace, name, kind), hpa_targets=hpa, namespace_info=namespace_info
        )
        return _types(recs, RecommendationType.OFF_PEAK_SCALING)

    def test_scalable_application_workload(self):
        assert [r.pod_name for r in self._offpeak()] == ["web"]

    @pytest.mark.parametrize("kind", ["DaemonSet", "Pod", "Job", "CronJob"])
    def test_non_scalable_kinds(self, kind):
        assert self._offpeak(kind=kind) == []

    def test_hpa_managed_workload(self):
        assert self._offpeak(hpa={("shop", "Deployment", "web")}) == []

    def test_operators_and_infrastructure(self):
        assert self._offpeak(name="shop-controller") == []
        assert self._offpeak(namespace="ingress-nginx", name="ingress-nginx") == []

    def test_opted_out_namespace(self):
        info = _namespaces("shop", annotations={"greenkube.io/ignore": "true"})
        assert self._offpeak(namespace_info=info) == []


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
