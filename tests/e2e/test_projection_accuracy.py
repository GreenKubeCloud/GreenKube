"""The e2e loop checks the v0.4 exit criterion: projected and measured allocated cost agree within 20%."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

FILTER = Path(__file__).resolve().parents[2] / "hack" / "e2e" / "projection-accuracy.jq"
LOOP = FILTER.with_name("loop.sh")

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _estimate(measured, projected, outcome="realized", quantity="cost") -> dict:
    return {"quantity": quantity, "annualized": measured, "projected_annual": projected, "outcome": outcome}


def _measurement(
    measurement_id: str,
    *,
    age: timedelta = timedelta(hours=1),
    outcome: str = "realized",
    estimates: list[dict] | None = None,
    recommendations: list[dict] | None = None,
) -> dict:
    return {
        "measurement_id": measurement_id,
        "decided_at": (NOW - age).isoformat(),
        "outcome": outcome,
        "estimates": estimates or [],
        "recommendations": recommendations or [],
    }


def _allocated(recommendation_id: int, measured, projected, outcome: str = "realized") -> dict:
    return {
        "recommendation_id": recommendation_id,
        "outcome": outcome,
        "estimates": [_estimate(measured, projected, outcome), _estimate(5.0, 1.0, outcome, quantity="co2e_g")],
    }


def _check(changes: list[dict], ids: list[int], tolerance: float = 0.2) -> dict:
    payload = {"recommendation_ids": ids, "tolerance": tolerance, "changes": changes}
    result = subprocess.run(
        ["jq", "-c", "-f", str(FILTER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_allocated_cost_within_tolerance_passes_and_records_the_ratio():
    changes = [{"change_id": "c1", "items": [_measurement("m1", recommendations=[_allocated(7, 115.0, 100.0)])]}]

    result = _check(changes, [7])

    assert result["status"] == "ok"
    (check,) = result["checks"]
    assert check["ratio"] == pytest.approx(0.15)
    assert check["measured_allocated"] == 115.0
    assert check["projected_allocated"] == 100.0
    assert check["source"] == "allocated"


def test_allocated_cost_beyond_tolerance_fails_in_both_directions():
    over = [{"change_id": "c1", "items": [_measurement("m1", recommendations=[_allocated(7, 125.0, 100.0)])]}]
    under = [{"change_id": "c1", "items": [_measurement("m1", recommendations=[_allocated(7, 70.0, 100.0)])]}]

    assert _check(over, [7])["status"] == "failed"
    assert _check(under, [7])["checks"][0]["ratio"] == pytest.approx(0.3)
    assert _check(under, [7])["status"] == "failed"


def test_the_latest_decided_measurement_counts():
    changes = [
        {
            "change_id": "c1",
            "items": [
                _measurement("m-new", age=timedelta(minutes=5), recommendations=[_allocated(7, 101.0, 100.0)]),
                _measurement("m-old", age=timedelta(hours=5), recommendations=[_allocated(7, 300.0, 100.0)]),
            ],
        }
    ]

    result = _check(changes, [7])

    assert result["status"] == "ok"
    assert result["checks"][0]["measurement_id"] == "m-new"


def test_inconclusive_measurement_is_a_warning_by_design():
    changes = [
        {
            "change_id": "c1",
            "items": [
                _measurement(
                    "m1", outcome="inconclusive", recommendations=[_allocated(7, 400.0, 100.0, outcome="inconclusive")]
                )
            ],
        }
    ]

    result = _check(changes, [7])

    assert result["status"] == "warning"
    assert result["checks"][0]["ratio"] is None
    assert "inconclusive" in result["checks"][0]["reason"]


def test_one_failure_among_several_recommendations_fails():
    changes = [
        {
            "change_id": "c1",
            "items": [_measurement("m1", recommendations=[_allocated(7, 100.0, 100.0), _allocated(8, 200.0, 100.0)])],
        }
    ]

    result = _check(changes, [7, 8])

    assert result["status"] == "failed"
    assert [check["status"] for check in result["checks"]] == ["ok", "failed"]


def test_single_recommendation_change_falls_back_to_the_change_estimate():
    changes = [{"change_id": "c1", "items": [_measurement("m1", estimates=[_estimate(90.0, 100.0)])]}]

    result = _check(changes, [7])

    assert result["status"] == "ok"
    assert result["checks"][0]["source"] == "change"


def test_missing_measurement_or_projection():
    assert _check([], [7])["status"] == "failed"
    assert _check([{"change_id": "c1", "items": []}], [7])["status"] == "failed"
    no_projection = [{"change_id": "c1", "items": [_measurement("m1", recommendations=[_allocated(7, 10.0, None)])]}]
    assert _check(no_projection, [7])["status"] == "warning"


def test_loop_runs_the_check_after_the_verdict():
    script = LOOP.read_text()

    verdict = script.index('evidence "${prefix}.verdict" ok')
    accuracy = script.index("projection-accuracy.jq")
    assert verdict < accuracy
    assert "/api/v2/changes/${change_id}/measurements" in script
    assert "E2E_PROJECTION_TOLERANCE:=0.2" in script
    assert 'evidence "${prefix}.projection_accuracy" warning' in script


def test_loop_skips_the_check_when_change_measurement_is_off():
    """The default quickstart records no change: the step is skipped, never a failure (e2e)."""
    script = LOOP.read_text()

    gate = script.index("CHANGE_MEASUREMENT_ENABLED")
    skipped = script.index('evidence "${prefix}.projection_accuracy" skipped')
    changes = script.index("/api/v2/changes?plan_id=${plan_id}")
    assert script.index('evidence "${prefix}.verdict" ok') < gate < skipped < changes
    assert 'get configmap "${GK_RELEASE}-config"' in script
