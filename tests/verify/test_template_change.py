"""An unrelated pod-template change does not end a measurement (MEA-08).

The measured saving depends on the containers' requests only: an image
deployment, an env change or a rollout restart with identical resources is a
mild (or moderate) ``template_change``, never a ``spec_change`` or a spec
drift; a template whose requests moved is (sim_specdrift.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from greenkube.verify.confounders import ConfounderDetector, confidence_score, is_contaminated
from greenkube.verify.digest import image_digest, pod_template_digest, resources_digest
from greenkube.verify.drift import detect_spec_drift
from greenkube.verify.model import WorkloadSpecObservation
from greenkube.verify.strategy import MeasurementSettings, RequestRightsizingStrategy
from tests.verify.fixtures import change_event  # pyrefly: ignore[missing-import]

COMPLETED = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def _template(image: str = "api:1", cpu: str = "300m", env: str = "a", sidecar_cpu: str = "50m") -> dict:
    return {
        "metadata": {"labels": {"app": "api", "pod-template-hash": "abc"}},
        "spec": {
            "containers": [
                {
                    "name": "api",
                    "image": image,
                    "env": [{"name": "MODE", "value": env}],
                    "resources": {"requests": {"cpu": cpu, "memory": "1Gi"}, "limits": {"memory": "1Gi"}},
                }
            ],
            "initContainers": [
                {"name": "proxy", "image": "proxy:1", "resources": {"requests": {"cpu": sidecar_cpu}}},
            ],
        },
    }


APPLIED = _template()


def _change():
    return change_event(COMPLETED).model_copy(
        update={
            "spec_digest_after": pod_template_digest(APPLIED),
            "resources_digest_after": resources_digest(APPLIED),
            "image_digest_after": image_digest(APPLIED),
        }
    )


def _observed(template: dict, offset: timedelta, uid: str = "uid-api") -> WorkloadSpecObservation:
    return WorkloadSpecObservation(
        namespace="prod",
        kind="Deployment",
        name="api",
        workload_uid=uid,
        spec_digest=pod_template_digest(template),
        resources_digest=resources_digest(template),
        image_digest=image_digest(template),
        observed_at=COMPLETED + offset,
    )


def test_resources_digest_follows_requests_and_limits_only():
    assert resources_digest(_template(image="api:2", env="b")) == resources_digest(APPLIED)
    assert pod_template_digest(_template(image="api:2")) != pod_template_digest(APPLIED)
    assert resources_digest(_template(cpu="1")) != resources_digest(APPLIED)
    # Init containers and native sidecars count.
    assert resources_digest(_template(sidecar_cpu="100m")) != resources_digest(APPLIED)
    assert image_digest(_template(image="api:2")) != image_digest(APPLIED)
    assert image_digest(_template(env="b")) == image_digest(APPLIED)
    assert resources_digest(None) is None and image_digest(None) is None


def _detect(observations):
    change = _change()
    plan = RequestRightsizingStrategy().required_windows(change, MeasurementSettings())
    return ConfounderDetector().detect(change, plan, observations=observations)


def test_an_image_deployment_during_the_measurement_is_a_template_change_not_a_contamination():
    """sim_specdrift day 3: an image deployment with identical resources."""
    findings = _detect([_observed(APPLIED, timedelta(minutes=5)), _observed(_template("api:2"), timedelta(days=3))])
    assert [(item.kind, item.severity) for item in findings] == [("template_change", "moderate")]
    assert not is_contaminated(findings)
    assert confidence_score(findings, 1.0, None) > 0.0


def test_an_env_change_is_a_mild_template_change():
    findings = _detect([_observed(_template(env="b"), timedelta(days=2))])
    assert [(item.kind, item.severity) for item in findings] == [("template_change", "mild")]


def test_a_request_change_during_the_measurement_still_contaminates():
    findings = _detect(
        [_observed(_template("api:2"), timedelta(days=2)), _observed(_template(cpu="1"), timedelta(days=4))]
    )
    assert ("spec_change", "severe") in [(item.kind, item.severity) for item in findings]
    assert is_contaminated(findings)


def test_a_template_change_without_resources_digests_stays_severe():
    """Observations stored before the digests existed cannot tell an image bump from a request change."""
    legacy = _observed(_template("api:2"), timedelta(days=2)).model_copy(update={"resources_digest": None})
    findings = _detect([legacy])
    assert [(item.kind, item.severity) for item in findings] == [("spec_change", "severe")]
    assert findings[0].evidence["resources_digest_known"] is False


def test_an_image_deployment_after_the_verdict_is_no_spec_drift():
    """sim_specdrift day 12: the change stays realized and keeps accruing."""
    assert detect_spec_drift(_change(), [_observed(_template("api:2"), timedelta(days=12))]) is None


def test_restoring_the_old_request_is_a_spec_drift():
    drift = detect_spec_drift(
        _change(),
        [_observed(_template("api:2"), timedelta(days=10)), _observed(_template("api:2", cpu="1"), timedelta(days=12))],
    )
    assert drift is not None and drift.event_type == "spec_drift" and drift.resources_known
    assert drift.accrual_end == COMPLETED + timedelta(days=12)


def test_a_recreated_workload_closes_only_when_its_resources_differ():
    assert detect_spec_drift(_change(), [_observed(APPLIED, timedelta(days=9), uid="uid-new")]) is None
    drift = detect_spec_drift(_change(), [_observed(_template(cpu="1"), timedelta(days=9), uid="uid-new")])
    assert drift is not None and drift.event_type == "workload_recreated"


def test_a_legacy_spec_drift_is_flagged_as_unverified():
    legacy = _observed(_template("api:2"), timedelta(days=10)).model_copy(update={"resources_digest": None})
    drift = detect_spec_drift(_change(), [legacy])
    assert drift is not None and drift.event_type == "spec_drift" and not drift.resources_known


def test_the_strategy_version_is_bumped():
    """A template change with identical resources no longer contaminates: outcomes change (MEA-08)."""
    assert RequestRightsizingStrategy.version == "2.5"
