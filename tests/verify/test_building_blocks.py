"""Hourly series, spec digests, load drivers, confounders and attribution (C1, C3, C4, C5)."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.verify.attribution import allocate
from greenkube.verify.confounders import ConfounderDetector, confidence_score, is_contaminated
from greenkube.verify.digest import hpa_digest, normalize_pod_template, pod_template_digest
from greenkube.verify.load_driver import LoadDriverResolver, PrometheusRangeReader, candidates_for
from greenkube.verify.model import (
    ChangeOutcome,
    Confounder,
    MeasurementState,
    SavingsEstimate,
    TimeWindow,
    WorkloadSpecObservation,
    is_allowed_transition,
)
from greenkube.verify.series import hourly_buckets, quantity_series
from greenkube.verify.strategy import MeasurementSettings, RequestRightsizingStrategy
from tests.verify.fixtures import change_event, hour_now, workload_rows  # pyrefly: ignore[missing-import]

STEP = 300


# ----------------------------------------------------------------------
# Hourly buckets
# ----------------------------------------------------------------------


def test_hourly_buckets_sum_replicas_and_rate_by_covered_time():
    start = hour_now() - timedelta(hours=3)
    window = TimeWindow(start=start, end=start + timedelta(hours=2))
    rows = workload_rows(window, cost=0.01, step=timedelta(minutes=5))
    buckets = hourly_buckets(rows, window, STEP)
    assert len(buckets) == 2
    bucket = buckets[0]
    assert bucket.coverage == pytest.approx(1.0)
    assert bucket.replicas == pytest.approx(2.0)
    assert bucket.core_hours == pytest.approx(2.0)  # 2 pods x 1000m
    assert bucket.gib_hours == pytest.approx(4.0)
    assert bucket.cost == pytest.approx(0.24)  # 2 pods x 12 rows x 0.01
    assert bucket.cost_cpu == pytest.approx(0.168)
    assert bucket.cpu_usage_cores == pytest.approx(0.4)


def test_hourly_buckets_ignore_untrusted_cost_and_report_gaps():
    start = hour_now() - timedelta(hours=3)
    window = TimeWindow(start=start, end=start + timedelta(hours=3))
    first_hour = TimeWindow(start=start, end=start + timedelta(hours=1))
    rows = workload_rows(first_hour, is_estimated=True)
    buckets = hourly_buckets(rows, window, STEP)
    assert [bucket.coverage for bucket in buckets] == [pytest.approx(1.0), 0.0, 0.0]
    assert buckets[0].cost is None and buckets[0].cost_coverage == 0.0
    assert quantity_series(buckets, "allocated_cost") == {}
    assert len(quantity_series(buckets, "core_hours")) == 1


# ----------------------------------------------------------------------
# Spec digests
# ----------------------------------------------------------------------


def _template(image="api:1", labels=None, env=None):
    return {
        "metadata": {"labels": labels or {"app": "api", "pod-template-hash": "abc"}},
        "spec": {"containers": [{"name": "api", "image": image, "env": env, "resources": {"requests": {"cpu": "1"}}}]},
    }


def test_pod_template_digest_ignores_controller_labels_and_empty_fields():
    assert pod_template_digest(_template()) == pod_template_digest(
        _template(labels={"app": "api", "pod-template-hash": "zzz"}, env=[])
    )
    normalized = normalize_pod_template(_template())
    assert normalized is not None and "pod-template-hash" not in normalized["metadata"]["labels"]
    assert pod_template_digest(_template()) != pod_template_digest(_template(image="api:2"))
    assert pod_template_digest(None) is None


def test_kubernetes_client_objects_are_normalized():
    class Obj:
        def __init__(self, data):
            self.data = data

        def to_dict(self):
            return self.data

    assert pod_template_digest(Obj(_template())) == pod_template_digest(_template())
    spec = {"min_replicas": 1, "max_replicas": 4, "scale_target_ref": {"name": "api"}}
    assert hpa_digest(spec) == hpa_digest({**spec, "scale_target_ref": {"name": "other"}})
    assert hpa_digest(spec) != hpa_digest({**spec, "max_replicas": 8})


# ----------------------------------------------------------------------
# Load drivers
# ----------------------------------------------------------------------


def _hours(window: TimeWindow, value=1.0, coverage=1.0):
    hours = []
    moment = window.start
    while moment < window.end:
        hours.append(moment)
        moment += timedelta(hours=1)
    kept = hours[: int(len(hours) * coverage)]
    return {hour: value for hour in kept}


def test_load_driver_resolution_order_and_coverage():
    change = change_event(hour_now() - timedelta(days=9), load_metric='sum(rate(my_requests{ns="$namespace"}[5m]))')
    assert [item.key for item in candidates_for(change)] == [
        "istio_rps",
        "linkerd_rps",
        "ingress_nginx_rps",
        "envoy_rps",
        "annotation",
        "cpu_usage",
    ]
    assert 'ns="prod"' in (candidates_for(change)[4].promql or "")
    assert 'envoy_cluster_name=~"prod_api_[^_]+"' in (candidates_for(change)[3].promql or "")
    dotted = candidates_for(change_event(hour_now(), name="api.v2"))[3].promql or ""
    assert 'envoy_cluster_name=~"prod_api\\\\.v2_[^_]+"' in dotted
    pre = TimeWindow(start=hour_now() - timedelta(days=16), end=hour_now() - timedelta(days=9))
    post = TimeWindow(start=hour_now() - timedelta(days=8), end=hour_now() - timedelta(days=1))
    resolver = LoadDriverResolver(min_coverage=0.8)
    # Istio is present but too sparse after the change; the annotation driver is complete.
    resolved = resolver.resolve(
        change,
        pre,
        post,
        {"istio_rps": _hours(pre), "annotation": _hours(pre), "cpu_usage": _hours(pre)},
        {"istio_rps": _hours(post, coverage=0.5), "annotation": _hours(post), "cpu_usage": _hours(post)},
    )
    assert resolved is not None and resolved.info.key == "annotation" and not resolved.info.weak
    only_cpu = resolver.resolve(change, pre, post, {"cpu_usage": _hours(pre)}, {"cpu_usage": _hours(post)})
    assert only_cpu is not None and only_cpu.info.key == "cpu_usage" and only_cpu.info.weak
    assert resolver.resolve(change, pre, post, {}, {}) is None


async def test_prometheus_reader_returns_hourly_means_for_the_previous_hour():
    window = TimeWindow(start=hour_now() - timedelta(hours=4), end=hour_now())
    calls = []

    async def query_range(promql, start, end, step):
        calls.append((promql, start, end, step))
        return [{"values": [[(window.start + timedelta(hours=2)).timestamp(), "12.5"], [0, "NaN"], ["bad", "1"]]}]

    reader = PrometheusRangeReader(query_range=query_range)
    values = await reader.hourly_means("sum(rate(x[5m]))", window)
    assert calls[0][0] == "avg_over_time((sum(rate(x[5m])))[1h:5m])" and calls[0][3] == 3600
    assert values == {window.start + timedelta(hours=1): 12.5}


async def test_resolver_series_reads_prometheus_candidates_and_cpu_usage():
    change = change_event(hour_now() - timedelta(days=9))
    window = TimeWindow(start=hour_now() - timedelta(hours=6), end=hour_now())

    async def query_range(promql, start, end, step):
        if "istio" in promql:
            return [{"values": [[start.timestamp(), "3"]]}]
        return []

    resolver = LoadDriverResolver(PrometheusRangeReader(query_range=query_range))
    buckets = hourly_buckets(workload_rows(window), window, STEP)
    series = await resolver.series(change, window, buckets)
    assert set(series) == {"istio_rps", "cpu_usage"}


async def test_resolver_names_the_prometheus_candidates_whose_query_failed():
    change = change_event(hour_now() - timedelta(days=9), load_metric="sum(rate(jobs_total[5m]))")
    window = TimeWindow(start=hour_now() - timedelta(hours=6), end=hour_now())

    async def query_range(promql, start, end, step):
        if "istio" in promql:
            return None  # Prometheus unreachable for this query
        return []  # reachable, no such series

    resolver = LoadDriverResolver(PrometheusRangeReader(query_range=query_range))
    buckets = hourly_buckets(workload_rows(window), window, STEP)
    collected = await resolver.collect(change, window, buckets)
    assert collected.unavailable == ["istio_rps"]
    assert set(collected.series) == {"cpu_usage"}
    # Without Prometheus configured, nothing is "unavailable": only CPU usage is a candidate.
    unconfigured = await LoadDriverResolver(PrometheusRangeReader(settings=SimpleNamespace())).collect(
        change, window, buckets
    )
    assert unconfigured.unavailable == [] and set(unconfigured.series) == {"cpu_usage"}


def test_the_change_lifecycle_only_allows_its_transitions():
    assert is_allowed_transition(MeasurementState.APPLIED, MeasurementState.MEASURING)
    assert is_allowed_transition(MeasurementState.MEASURING, MeasurementState.MEASURING)
    assert is_allowed_transition(MeasurementState.REALIZED, MeasurementState.REGRESSED)
    assert not is_allowed_transition(MeasurementState.CLOSED, MeasurementState.MEASURING)
    assert not is_allowed_transition(MeasurementState.REALIZED, MeasurementState.MEASURING)
    assert not is_allowed_transition(MeasurementState.REALIZED, MeasurementState.REALIZED)
    assert not is_allowed_transition(MeasurementState.APPLIED, MeasurementState.REALIZED)


# ----------------------------------------------------------------------
# Confounders
# ----------------------------------------------------------------------


def _plan(change):
    return RequestRightsizingStrategy().required_windows(change, MeasurementSettings())


def test_another_change_during_the_measurement_contaminates_it():
    completed = hour_now() - timedelta(days=9)
    change = change_event(completed)
    plan = _plan(change)
    later = change_event(completed + timedelta(days=2), change_id="change-2")
    earlier = change_event(completed - timedelta(days=3), change_id="change-0")
    findings = ConfounderDetector().detect(change, plan, other_changes=[change, later, earlier])
    assert [(item.kind, item.severity) for item in findings] == [
        ("overlapping_change", "severe"),
        ("change_in_baseline", "moderate"),
    ]
    assert is_contaminated(findings)
    assert confidence_score(findings, 1.0, None) == 0.0


def test_spec_hpa_and_recreation_confounders():
    completed = hour_now() - timedelta(days=9)
    change = change_event(completed)
    plan = _plan(change)

    def observed(offset, **values):
        base: dict[str, Any] = {"namespace": "prod", "kind": "Deployment", "name": "api", "workload_uid": "uid-api"}
        return WorkloadSpecObservation(**{**base, **values}, observed_at=completed + offset)

    observations = [
        observed(timedelta(days=-3), spec_digest="digest-before", hpa_digest="h1"),
        observed(timedelta(minutes=10), spec_digest="digest-after", hpa_digest="h1"),
        observed(timedelta(days=1), spec_digest="digest-after", hpa_digest="h2"),
        observed(timedelta(days=2), spec_digest="digest-image-bump", hpa_digest="h2"),
        observed(timedelta(days=3), spec_digest="digest-after", hpa_digest="h2", workload_uid="uid-new"),
    ]
    kinds = {
        (item.kind, item.severity) for item in ConfounderDetector().detect(change, plan, observations=observations)
    }
    assert kinds == {
        # The HPA edit landed after the rollout: replicas, so the allocated cost, moved without the change.
        ("hpa_change", "severe"),
        ("spec_change", "severe"),
        ("workload_recreated", "severe"),
    }


def test_replica_price_and_instance_type_confounders_lower_confidence():
    completed = hour_now() - timedelta(days=9)
    change = change_event(completed)
    plan = _plan(change)
    pre = hourly_buckets(workload_rows(plan.pre, instance_type="m5.large"), plan.pre, STEP)
    # Twice the replicas, a 15% higher RAM price (RAM requests unchanged) and half the pods on new instance types.
    post_rows = workload_rows(
        plan.post, pods=("a", "b"), cpu_request=400, ram_price=0.0115, instance_type="m5.large"
    ) + workload_rows(plan.post, pods=("c", "d"), cpu_request=400, ram_price=0.0115, instance_type="c6g.large")
    post = hourly_buckets(post_rows, plan.post, STEP)
    findings = ConfounderDetector().detect(change, plan, pre=pre, post=post)
    assert sorted((item.kind, item.evidence.get("resource")) for item in findings) == [
        ("instance_type_change", None),
        ("price_card_change", "memory"),
        ("replica_shift", None),
    ]
    assert not is_contaminated(findings)
    assert confidence_score(findings, 1.0, None) == pytest.approx(1.0 - 3 * 0.15 - 0.10)


def test_hpa_edit_in_the_baseline_only_lowers_confidence():
    completed = hour_now() - timedelta(days=9)
    change = change_event(completed)
    plan = _plan(change)
    base = {"namespace": "prod", "kind": "Deployment", "name": "api", "workload_uid": "uid-api"}
    observations = [
        WorkloadSpecObservation(
            **base, spec_digest="digest-before", hpa_digest="h1", observed_at=completed - timedelta(days=5)
        ),
        WorkloadSpecObservation(
            **base, spec_digest="digest-before", hpa_digest="h2", observed_at=completed - timedelta(days=3)
        ),
        WorkloadSpecObservation(
            **base, spec_digest="digest-after", hpa_digest="h2", observed_at=completed + timedelta(minutes=5)
        ),
    ]
    findings = ConfounderDetector().detect(change, plan, observations=observations)
    assert [(item.kind, item.severity) for item in findings] == [("hpa_change", "moderate")]
    assert not is_contaminated(findings)


def test_large_price_or_instance_type_shifts_contaminate_the_measurement():
    completed = hour_now() - timedelta(days=9)
    change = change_event(completed)
    plan = _plan(change)
    pre = hourly_buckets(workload_rows(plan.pre, instance_type="m5.large"), plan.pre, STEP)
    # A 30% higher RAM price on untouched RAM requests, and every pod on new hardware.
    post = hourly_buckets(
        workload_rows(plan.post, cpu_request=400, ram_price=0.013, instance_type="c6g.large"), plan.post, STEP
    )
    findings = ConfounderDetector().detect(change, plan, pre=pre, post=post)
    assert sorted((item.kind, item.severity) for item in findings) == [
        ("instance_type_change", "severe"),
        ("price_card_change", "severe"),
    ]
    assert is_contaminated(findings)


def test_steady_workload_has_no_confounders():
    completed = hour_now() - timedelta(days=9)
    change = change_event(completed)
    plan = _plan(change)
    pre = hourly_buckets(workload_rows(plan.pre), plan.pre, STEP)
    post = hourly_buckets(workload_rows(plan.post, cpu_request=300), plan.post, STEP)
    # The CPU requests the change cut are not a price signal; the untouched RAM price is stable.
    assert ConfounderDetector().detect(change, plan, pre=pre, post=post) == []


# ----------------------------------------------------------------------
# Attribution (C5)
# ----------------------------------------------------------------------


def _record(rec_id, rec_type, **values):
    base: dict[str, Any] = dict(
        id=rec_id,
        type=rec_type,
        description="x",
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        status=RecommendationStatus.VERIFYING,
    )
    return RecommendationRecord(**{**base, **values})


def _estimate(quantity, rate=1.0):
    return SavingsEstimate(
        quantity=quantity,
        unit="currency",
        claimed=quantity == "allocated_cost",
        rate_per_hour=rate,
        aligned_hours=150,
        annualized=rate * 8766,
        outcome=ChangeOutcome.REALIZED,
    )


def test_cost_components_split_cpu_and_memory_records_by_request_reduction():
    records = [
        _record(
            1,
            RecommendationType.RIGHTSIZING_CPU,
            current_cpu_request_millicores=1000,
            recommended_cpu_request_millicores=400,
        ),
        _record(
            2,
            RecommendationType.RIGHTSIZING_CPU,
            current_cpu_request_millicores=500,
            recommended_cpu_request_millicores=300,
        ),
        _record(
            3, RecommendationType.RIGHTSIZING_MEMORY, current_memory_request_bytes=4, recommended_memory_request_bytes=2
        ),
    ]
    # The components add up to the net allocated saving (no cap).
    shares = allocate(records, [_estimate("allocated_cost", 2.0), _estimate("cost_cpu"), _estimate("cost_ram")])
    by_id = {share.record.id: share for share in shares}
    assert by_id[1].quantity == by_id[2].quantity == "cost_cpu"
    assert by_id[1].share == pytest.approx(0.75) and by_id[2].share == pytest.approx(0.25)
    assert by_id[3].quantity == "cost_ram" and by_id[3].share == pytest.approx(1.0)


def test_without_components_records_share_the_total_by_projection():
    records = [
        _record(1, RecommendationType.RIGHTSIZING_CPU, potential_savings_cost=300.0),
        _record(2, RecommendationType.RIGHTSIZING_MEMORY, potential_savings_cost=100.0),
    ]
    shares = allocate(records, [_estimate("allocated_cost")])
    assert [(share.quantity, share.basis, share.share) for share in shares] == [
        ("allocated_cost", "projected_share", pytest.approx(0.75)),
        ("allocated_cost", "projected_share", pytest.approx(0.25)),
    ]
    single = allocate(records[:1], [_estimate("allocated_cost")])
    assert [(share.basis, share.share) for share in single] == [("total", 1.0)]


def test_confounder_model_is_serializable():
    finding = Confounder(kind="hpa_change", severity="moderate", evidence={"x": 1})
    assert SimpleNamespace(**finding.model_dump()).severity == "moderate"
