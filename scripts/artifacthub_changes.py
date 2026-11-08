"""Generate the Chart.yaml ``artifacthub.io/changes`` annotation from CHANGELOG.md.

Usage:
    python scripts/artifacthub_changes.py 0.4.0
    python scripts/artifacthub_changes.py Unreleased --chart /tmp/Chart.yaml

The release workflow runs it before packaging the chart, so the Artifact Hub
changes list always describes the released version (roadmap N10).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

ANNOTATION = "artifacthub.io/changes"

# Keep a Changelog section -> Artifact Hub change kind.
KINDS = {
    "added": "added",
    "changed": "changed",
    "deprecated": "deprecated",
    "removed": "removed",
    "fixed": "fixed",
    "security": "security",
}

MAX_DESCRIPTION_LENGTH = 200
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BOLD_TITLE = re.compile(r"^\*\*(?P<title>.+?)\*\*\s*:?\s*(?P<rest>.*)$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*(])")


class ChangelogError(ValueError):
    """The CHANGELOG has no usable section for the requested version."""


def version_section(changelog: str, version: str) -> str:
    """Return the body of ``## [version]`` (``Unreleased`` is accepted)."""
    header = re.compile(rf"^## \[{re.escape(version)}\][^\n]*$", re.MULTILINE)
    match = header.search(changelog)
    if match is None:
        raise ChangelogError(f"CHANGELOG.md has no section for version {version}")
    following = re.compile(r"^## \[", re.MULTILINE).search(changelog, match.end())
    return changelog[match.end() : following.start() if following else len(changelog)]


def _clean(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = text.replace("**", "")
    return " ".join(text.split())


def _truncate(text: str) -> str:
    if len(text) <= MAX_DESCRIPTION_LENGTH:
        return text
    cut = text[: MAX_DESCRIPTION_LENGTH - 1].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{cut}…"


def summarize(bullet: str) -> str:
    """Turn one changelog bullet into a short Artifact Hub description."""
    match = _BOLD_TITLE.match(bullet.strip())
    if match:
        title = _clean(match.group("title")).rstrip(":").strip()
        rest = _clean(match.group("rest"))
        first_sentence = _SENTENCE_END.split(rest, maxsplit=1)[0].strip() if rest else ""
        description = f"{title}: {first_sentence}" if first_sentence else title
    else:
        description = _SENTENCE_END.split(_clean(bullet), maxsplit=1)[0]
    return _truncate(description.strip())


def changes(section: str) -> list[dict[str, str]]:
    """Return ``[{"kind", "description"}]`` for the top-level bullets of a version section."""
    entries: list[dict[str, str]] = []
    kind: str | None = None
    for line in section.splitlines():
        heading = re.match(r"^### (\w+)", line)
        if heading:
            kind = KINDS.get(heading.group(1).lower())
            continue
        if kind and line.startswith("- "):
            description = summarize(line[2:])
            if description:
                entries.append({"kind": kind, "description": description})
    if not entries:
        raise ChangelogError("The CHANGELOG section has no Added/Changed/Deprecated/Removed/Fixed/Security entries")
    return entries


def render(entries: list[dict[str, str]]) -> str:
    lines = []
    for entry in entries:
        lines.append(f"- kind: {entry['kind']}")
        lines.append(f"  description: {json.dumps(entry['description'], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def update_chart(chart_path: Path, annotation: str) -> None:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    chart = yaml.load(chart_path.read_text(encoding="utf-8"))
    chart.setdefault("annotations", {})[ANNOTATION] = LiteralScalarString(annotation)
    with chart_path.open("w", encoding="utf-8") as stream:
        yaml.dump(chart, stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="Version to describe, for example 0.4.0 or Unreleased")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--chart", type=Path, default=Path("helm-chart/Chart.yaml"))
    args = parser.parse_args(argv)
    version = args.version.removeprefix("v")
    try:
        entries = changes(version_section(args.changelog.read_text(encoding="utf-8"), version))
    except ChangelogError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    update_chart(args.chart, render(entries))
    print(f"Wrote {len(entries)} Artifact Hub changes for {version} to {args.chart}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
