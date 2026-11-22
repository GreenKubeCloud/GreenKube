"""Tests for scripts/artifacthub_changes.py (roadmap N10)."""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
_spec = importlib.util.spec_from_file_location("artifacthub_changes", ROOT / "scripts" / "artifacthub_changes.py")
assert _spec is not None and _spec.loader is not None
artifacthub_changes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = artifacthub_changes
_spec.loader.exec_module(artifacthub_changes)

CHANGELOG = (
    """# Changelog

## [Unreleased]

### Added
- **Next thing:** Not released yet.

## [1.2.0] — 2027-01-01

### Added
- **Credentials bootstrap:** Generated credentials are created once. Rendering is stable.
  - A nested detail that must be ignored.
- A plain bullet with a [link](https://example.com). And a second sentence.

### Security
- **CORS:** Same-origin by default (BUG-59).

### Notes
- Not an Artifact Hub kind.

### Fixed
- **Long:** """
    + "word " * 80
    + """

## [1.1.0] — 2026-12-01

### Removed
- **Old thing:** Gone.
"""
)


def test_version_section_stops_at_the_next_version():
    section = artifacthub_changes.version_section(CHANGELOG, "1.2.0")

    assert "Credentials bootstrap" in section
    assert "Old thing" not in section
    assert "Next thing" not in section


def test_changes_maps_sections_and_summarizes_top_level_bullets():
    entries = artifacthub_changes.changes(artifacthub_changes.version_section(CHANGELOG, "1.2.0"))

    assert entries[:3] == [
        {"kind": "added", "description": "Credentials bootstrap: Generated credentials are created once."},
        {"kind": "added", "description": "A plain bullet with a link."},
        {"kind": "security", "description": "CORS: Same-origin by default (BUG-59)."},
    ]
    assert [entry["kind"] for entry in entries] == ["added", "added", "security", "fixed"]
    assert len(entries[-1]["description"]) <= artifacthub_changes.MAX_DESCRIPTION_LENGTH
    assert entries[-1]["description"].endswith("…")


def test_unknown_version_is_an_error():
    with pytest.raises(artifacthub_changes.ChangelogError):
        artifacthub_changes.version_section(CHANGELOG, "9.9.9")


def test_main_rewrites_only_the_changes_annotation(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    chart = tmp_path / "Chart.yaml"
    chart.write_text(
        "apiVersion: v2\n"
        "name: greenkube\n"
        "version: 1.2.0\n"
        "annotations:\n"
        "  artifacthub.io/category: integration-delivery\n"
        "  artifacthub.io/changes: |\n"
        "    - kind: fixed\n"
        '      description: "stale"\n',
        encoding="utf-8",
    )

    assert artifacthub_changes.main(["v1.2.0", "--changelog", str(changelog), "--chart", str(chart)]) == 0

    text = chart.read_text(encoding="utf-8")
    assert "artifacthub.io/changes: |" in text
    loaded = yaml.safe_load(text)
    assert loaded["annotations"]["artifacthub.io/category"] == "integration-delivery"
    changes = yaml.safe_load(loaded["annotations"]["artifacthub.io/changes"])
    assert changes[0] == {
        "kind": "added",
        "description": "Credentials bootstrap: Generated credentials are created once.",
    }
    assert "stale" not in text


def test_main_fails_without_a_version_section(tmp_path, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")

    assert artifacthub_changes.main(["2.0.0", "--changelog", str(changelog), "--chart", str(tmp_path / "x")]) == 1
    assert "no section for version 2.0.0" in capsys.readouterr().err


def test_committed_chart_lists_the_current_release_changes():
    chart = yaml.safe_load((ROOT / "helm-chart" / "Chart.yaml").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    expected = artifacthub_changes.changes(artifacthub_changes.version_section(changelog, chart["version"]))

    assert yaml.safe_load(chart["annotations"]["artifacthub.io/changes"]) == expected
