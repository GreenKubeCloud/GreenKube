"""Tests for scripts/sync_version.py: one version source of truth (roadmap F-10).

``pyproject.toml`` holds the version. The API (``greenkube.__version__``,
shown by the UI banner), the chart ``version``/``appVersion``, the chart image
tag and the frontend package must all carry it; the check fails otherwise.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location("sync_version", ROOT / "scripts" / "sync_version.py")
assert _spec is not None and _spec.loader is not None
sync_version = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync_version
_spec.loader.exec_module(sync_version)

FILES = (
    "pyproject.toml",
    "src/greenkube/__init__.py",
    "helm-chart/Chart.yaml",
    "helm-chart/values.yaml",
    "frontend/package.json",
)


def _copy_tree(tmp_path: Path) -> Path:
    for rel in FILES:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    return tmp_path


def test_repository_versions_agree():
    """The API, UI banner, chart and frontend versions match pyproject.toml."""
    assert sync_version.check_versions(ROOT) == []


def test_api_version_is_the_project_version():
    """The UI banner shows the API version, which is the project version."""
    from greenkube import __version__

    assert __version__ == sync_version.get_project_version(ROOT)


@pytest.mark.parametrize(
    ("rel", "old", "new"),
    [
        ("helm-chart/Chart.yaml", "appVersion: ", "appVersion: 9.9.9\nx-old: "),
        ("src/greenkube/__init__.py", '__version__ = "', '__version__ = "9.9.9" #'),
    ],
)
def test_check_reports_a_diverging_file(tmp_path, rel, old, new):
    root = _copy_tree(tmp_path)
    path = root / rel
    path.write_text(path.read_text().replace(old, new, 1))

    problems = sync_version.check_versions(root)

    assert problems and any(rel in problem for problem in problems)


def test_check_mode_exits_non_zero_on_divergence(tmp_path):
    root = _copy_tree(tmp_path)
    chart = root / "helm-chart/Chart.yaml"
    chart.write_text(chart.read_text().replace("appVersion: ", "appVersion: 9.9.9\nx-old: ", 1))

    assert sync_version.main(["--check", "--root", str(root)]) == 1
    assert sync_version.main(["--check", "--root", str(ROOT)]) == 0
