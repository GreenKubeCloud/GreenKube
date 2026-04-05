# tests/cli/test_cli_cicd.py
"""
Tests for CI/CD pipeline features of the GreenKube CLI.

Covers:
- --no-color flag (disables Rich formatting)
- NO_COLOR env var support
- --fail-on-recommendations (non-zero exit if recommendations exist)
- --fail-on-co2-threshold (non-zero exit if any pod exceeds threshold)
- --fail-on-cost-threshold (non-zero exit if any pod exceeds threshold)
"""

from unittest.mock import AsyncMock, MagicMock

from typer.testing import CliRunner

import greenkube.cli.recommend as recommend_mod
import greenkube.cli.report as report_mod
from greenkube.cli import app
from greenkube.models.metrics import CombinedMetric, Recommendation, RecommendationType

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_metrics(co2e=10.0, cost=1.0):
    return [
        CombinedMetric(
            pod_name="pod-a",
            namespace="ns1",
            total_cost=cost,
            co2e_grams=co2e,
            joules=100.0,
        )
    ]


def make_recommendation():
    return Recommendation(
        type=RecommendationType.RIGHTSIZING_CPU,
        pod_name="pod-a",
        namespace="ns1",
        description="Reduce CPU request",
    )


# ---------------------------------------------------------------------------
# --no-color / NO_COLOR
# ---------------------------------------------------------------------------


def test_no_color_flag_disables_rich_markup(monkeypatch):
    """--no-color should set NO_COLOR env var so Rich emits plain text."""
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=make_metrics())
    monkeypatch.setattr(report_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    result = runner.invoke(app, ["--no-color", "report"])
    assert result.exit_code == 0
    # Rich tables with markup would contain ANSI escape sequences or styled
    # brackets; plain text should not contain ESC character.
    assert "\x1b[" not in result.output


def test_no_color_env_var_disables_rich_markup(monkeypatch):
    """NO_COLOR=1 env var should also disable Rich formatting."""
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=make_metrics())
    monkeypatch.setattr(report_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    result = runner.invoke(app, ["report"], env={"NO_COLOR": "1"})
    assert result.exit_code == 0
    assert "\x1b[" not in result.output


# ---------------------------------------------------------------------------
# --fail-on-recommendations
# ---------------------------------------------------------------------------


def test_fail_on_recommendations_exits_nonzero_when_recommendations_exist(monkeypatch):
    """Exit code should be 1 when recommendations are found and --fail-on-recommendations is set."""
    items = make_metrics()
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=items)
    monkeypatch.setattr(recommend_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    dummy_node_repo = MagicMock()
    dummy_node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    monkeypatch.setattr(recommend_mod, "get_node_repository", lambda: dummy_node_repo)

    dummy_recommender = MagicMock()
    dummy_recommender.generate_recommendations = MagicMock(return_value=[make_recommendation()])
    monkeypatch.setattr(recommend_mod, "Recommender", lambda: dummy_recommender)

    result = runner.invoke(app, ["recommend", "--fail-on-recommendations"])
    assert result.exit_code == 1


def test_fail_on_recommendations_exits_zero_when_no_recommendations(monkeypatch):
    """Exit code should be 0 when no recommendations exist, even with --fail-on-recommendations."""
    items = make_metrics()
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=items)
    monkeypatch.setattr(recommend_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    dummy_node_repo = MagicMock()
    dummy_node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    monkeypatch.setattr(recommend_mod, "get_node_repository", lambda: dummy_node_repo)

    dummy_recommender = MagicMock()
    dummy_recommender.generate_recommendations = MagicMock(return_value=[])
    monkeypatch.setattr(recommend_mod, "Recommender", lambda: dummy_recommender)

    result = runner.invoke(app, ["recommend", "--fail-on-recommendations"])
    assert result.exit_code == 0


def test_fail_on_recommendations_not_set_exits_zero_with_recommendations(monkeypatch):
    """Without the flag, exit code is 0 even if recommendations are present."""
    items = make_metrics()
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=items)
    monkeypatch.setattr(recommend_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    dummy_node_repo = MagicMock()
    dummy_node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    monkeypatch.setattr(recommend_mod, "get_node_repository", lambda: dummy_node_repo)

    dummy_recommender = MagicMock()
    dummy_recommender.generate_recommendations = MagicMock(return_value=[make_recommendation()])
    monkeypatch.setattr(recommend_mod, "Recommender", lambda: dummy_recommender)

    result = runner.invoke(app, ["recommend"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --fail-on-co2-threshold
# ---------------------------------------------------------------------------


def test_fail_on_co2_threshold_exits_nonzero_when_exceeded(monkeypatch):
    """Exit code 1 when a pod's CO2e exceeds the threshold."""
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=make_metrics(co2e=150.0))
    monkeypatch.setattr(report_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    result = runner.invoke(app, ["report", "--fail-on-co2-threshold", "100.0"])
    assert result.exit_code == 1


def test_fail_on_co2_threshold_exits_zero_when_not_exceeded(monkeypatch):
    """Exit code 0 when all pods are below the CO2e threshold."""
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=make_metrics(co2e=50.0))
    monkeypatch.setattr(report_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    result = runner.invoke(app, ["report", "--fail-on-co2-threshold", "100.0"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --fail-on-cost-threshold
# ---------------------------------------------------------------------------


def test_fail_on_cost_threshold_exits_nonzero_when_exceeded(monkeypatch):
    """Exit code 1 when a pod's cost exceeds the threshold."""
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=make_metrics(cost=5.0))
    monkeypatch.setattr(report_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    result = runner.invoke(app, ["report", "--fail-on-cost-threshold", "3.0"])
    assert result.exit_code == 1


def test_fail_on_cost_threshold_exits_zero_when_not_exceeded(monkeypatch):
    """Exit code 0 when all pods are below the cost threshold."""
    dummy_repo = MagicMock()
    dummy_repo.read_combined_metrics = AsyncMock(return_value=make_metrics(cost=1.0))
    monkeypatch.setattr(report_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    result = runner.invoke(app, ["report", "--fail-on-cost-threshold", "3.0"])
    assert result.exit_code == 0


def test_fail_on_co2_and_cost_threshold_both_respected(monkeypatch):
    """Both thresholds can be combined; exit 1 if either is violated."""
    dummy_repo = MagicMock()
    # CO2 is fine, cost is exceeded
    dummy_repo.read_combined_metrics = AsyncMock(return_value=make_metrics(co2e=50.0, cost=10.0))
    monkeypatch.setattr(report_mod, "get_combined_metrics_repository", lambda: dummy_repo)

    result = runner.invoke(app, ["report", "--fail-on-co2-threshold", "100.0", "--fail-on-cost-threshold", "5.0"])
    assert result.exit_code == 1
