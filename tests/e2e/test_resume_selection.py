"""The e2e harness resumes only from an unfinished pull request, never from an old merged one."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

FILTER = Path(__file__).resolve().parents[2] / "hack" / "e2e" / "resumable-pr.jq"
LOOP = FILTER.with_name("loop.sh")

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(delta: timedelta, *, fraction: bool = False) -> str:
    value = NOW - delta
    text = value.isoformat()
    if fraction:
        text = value.replace(microsecond=123456).isoformat()
    return text.replace("+00:00", "Z") if not fraction else text


def _pr(number: int, status: str, plan: str, created: timedelta, merged: timedelta | None = None) -> dict:
    return {
        "pr_number": number,
        "status": status,
        "plan_id": plan,
        "created_at": _iso(created),
        "merged_at": _iso(merged, fraction=True) if merged is not None else None,
    }


def _select(prs: list[dict], plans: dict[str, str], max_age: int = 6 * 3600) -> dict | None:
    payload = {"prs": prs, "plans": plans, "now": NOW.timestamp(), "max_age": max_age}
    result = subprocess.run(
        ["jq", "-ce", "-f", str(FILTER)], input=json.dumps(payload), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        assert result.returncode == 4, result.stderr  # no resumable pull request
        return None
    return json.loads(result.stdout)


def test_an_old_merged_pull_request_of_a_completed_loop_is_ignored():
    prs = [_pr(3, "merged", "plan-old", timedelta(days=2), merged=timedelta(days=2))]
    assert _select(prs, {"plan-old": "applied"}) is None
    # Merged long ago, even if its plan never got further.
    assert _select(prs, {"plan-old": "merged"}) is None


def test_a_recent_merge_not_yet_applied_is_resumed():
    prs = [
        _pr(3, "merged", "plan-old", timedelta(days=2), merged=timedelta(days=2)),
        _pr(4, "merged", "plan-new", timedelta(hours=1), merged=timedelta(minutes=30)),
    ]
    assert _select(prs, {"plan-old": "applied", "plan-new": "synced"})["pr_number"] == 4
    assert _select(prs, {"plan-old": "applied", "plan-new": "applied"}) is None


def test_an_open_pull_request_is_resumed_and_the_newest_wins():
    prs = [
        _pr(4, "merged", "plan-a", timedelta(hours=2), merged=timedelta(hours=1)),
        _pr(5, "open", "plan-b", timedelta(minutes=10)),
        _pr(5, "open", "plan-b", timedelta(minutes=10)),
        _pr(2, "closed", "plan-c", timedelta(minutes=1)),
        {"pr_number": 9, "status": "open", "plan_id": None, "created_at": _iso(timedelta(minutes=1))},
    ]
    assert _select(prs, {"plan-a": "merged"})["pr_number"] == 5


def test_loop_uses_the_filter():
    script = LOOP.read_text()
    assert "resumable-pr.jq" in script and "E2E_RESUME_MAX_AGE" in script
