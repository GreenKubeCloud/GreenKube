"""Bare-metal LoadBalancer allocators carry no cloud bill (MetalLB, Cilium LB-IPAM, kube-vip)."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.collectors.lb_collector import OrphanedLoadBalancer, bare_metal_allocator
from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import OrphanedLBAnalyzer
from greenkube.core.optimization.context import OptimizationContext


def _service(*, annotations=None, labels=None, load_balancer_class=None, conditions=None):
    spec = {"type": "LoadBalancer", "selector": {"app": "old"}}
    if load_balancer_class:
        spec["loadBalancerClass"] = load_balancer_class
    status = {"loadBalancer": {"ingress": [{"ip": "192.168.49.240"}]}}
    if conditions:
        status["conditions"] = conditions
    return {
        "metadata": {"name": "old", "namespace": "legacy", "annotations": annotations or {}, "labels": labels or {}},
        "spec": spec,
        "status": status,
    }


@pytest.mark.parametrize(
    ("service", "allocator"),
    [
        (_service(annotations={"metallb.universe.tf/ip-allocated-from-pool": "default"}), "MetalLB"),
        (_service(annotations={"metallb.io/ip-allocated-from-pool": "default"}), "MetalLB"),
        (_service(load_balancer_class="metallb.io/metallb"), "MetalLB"),
        (_service(annotations={"lbipam.cilium.io/ips": "10.0.0.5"}), "Cilium LB-IPAM"),
        (_service(load_balancer_class="io.cilium/l2-announcer"), "Cilium LB-IPAM"),
        (_service(conditions=[{"type": "cilium.io/IPAMRequestSatisfied", "status": "True"}]), "Cilium LB-IPAM"),
        (_service(annotations={"kube-vip.io/vipHost": "node-1"}), "kube-vip"),
        (_service(labels={"implementation": "kube-vip"}), "kube-vip"),
        (_service(load_balancer_class="kube-vip.io/kube-vip-class"), "kube-vip"),
        (_service(), None),
        (_service(annotations={"service.beta.kubernetes.io/aws-load-balancer-type": "nlb"}), None),
    ],
)
def test_bare_metal_allocator_is_detected_from_the_service(service, allocator):
    assert bare_metal_allocator(service) == allocator


def _analyze(**lb_fields):
    first_seen = datetime.now(timezone.utc) - timedelta(days=30)
    lb = OrphanedLoadBalancer(
        name="old", namespace="legacy", evidence=["no consumer"], first_seen_at=first_seen, **lb_fields
    )
    context = OptimizationContext(config=Config(), load_balancers=[lb])
    (rec,) = OrphanedLBAnalyzer(context.config).analyze(context)
    return rec


def _assumptions(rec):
    assert rec.impact is not None
    return rec.impact["assumptions"]


def test_bare_metal_load_balancer_projects_no_cost_saving_and_says_why():
    rec = _analyze(bare_metal_allocator="MetalLB")
    assert rec.potential_savings_cost == 0.0
    assert _assumptions(rec)["cost_source"] == "none"
    assert _assumptions(rec)["cost_detail"] == "bare_metal_load_balancer:MetalLB"
    assert "No cost saving" in rec.description and "MetalLB" in rec.description
    assert "stops its billing" not in rec.description


def test_opencost_cost_still_wins_for_a_bare_metal_allocator():
    rec = _analyze(bare_metal_allocator="MetalLB", annual_cost=60.0)
    assert rec.potential_savings_cost == 60.0
    assert _assumptions(rec)["cost_source"] == "opencost"


def test_cloud_load_balancer_keeps_the_configured_estimate():
    rec = _analyze()
    assert rec.potential_savings_cost == pytest.approx(Config().LOAD_BALANCER_COST_PER_MONTH * 12)
    assert _assumptions(rec)["cost_source"] == "custom_rate"
