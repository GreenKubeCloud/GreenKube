"""The operations dashboard reads only metric families GreenKube exposes (B.7)."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "dashboards" / "greenkube-operations.json"
METRIC = re.compile(r"\bgreenkube_[a-z0-9_]+")


def _dashboard() -> dict:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _exprs(dashboard: dict) -> list[str]:
    return [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])]


def _declared_families() -> set[str]:
    """Metric family names declared as string literals in the source tree."""
    names: set[str] = set()
    for path in (ROOT / "src" / "greenkube").rglob("*.py"):
        names.update(re.findall(r'"(greenkube_[a-z0-9_]+)"', path.read_text(encoding="utf-8")))
    return names


def test_dashboard_is_the_small_operations_dashboard():
    dashboard = _dashboard()
    assert dashboard["uid"] == "greenkube-operations"
    panels = [panel for panel in dashboard["panels"] if panel["type"] != "row"]
    assert 0 < len(panels) <= 25
    assert len({panel["id"] for panel in dashboard["panels"]}) == len(dashboard["panels"])
    for panel in dashboard["panels"]:
        grid = panel["gridPos"]
        assert grid["x"] + grid["w"] <= 24


def test_every_queried_metric_is_exposed():
    declared = _declared_families()
    used = {name for expr in _exprs(_dashboard()) for name in METRIC.findall(expr)}
    assert used
    missing = {name for name in used if name not in declared and name.removesuffix("_total") not in declared}
    assert not missing, f"dashboard queries metrics GreenKube does not expose: {sorted(missing)}"


def test_no_panel_reads_per_pod_or_footprint_series():
    for expr in _exprs(_dashboard()):
        assert "greenkube_pod_" not in expr
        assert "greenkube_namespace_" not in expr
        assert "sustainability" not in expr
