"""Propagate the project version from pyproject.toml, or check that it was propagated.

``pyproject.toml`` is the single source of truth. ``python scripts/sync_version.py``
writes it to every file that carries the version; ``--check`` writes nothing and
exits 1 when the API version (shown by the UI banner), the chart ``version`` or
``appVersion``, the chart image tag or the frontend package disagree with it.
"""

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import List, Optional

from ruamel.yaml import YAML


def get_project_version(root: Path = Path(".")) -> str:
    """Reads the version from pyproject.toml."""
    with open(Path(root) / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def read_versions(root: Path) -> dict:
    """Return ``{"file: field": version}`` for every place that must carry the project version."""
    root = Path(root)
    yaml = YAML(typ="safe")
    init_text = (root / "src/greenkube/__init__.py").read_text()
    match = re.search(r'^__version__ = "([^"]+)"', init_text, flags=re.MULTILINE)
    chart = yaml.load((root / "helm-chart/Chart.yaml").read_text())
    values = yaml.load((root / "helm-chart/values.yaml").read_text())
    images = yaml.load(chart.get("annotations", {}).get("artifacthub.io/images", "") or "[]") or []
    package = json.loads((root / "frontend/package.json").read_text())
    versions = {
        "src/greenkube/__init__.py: __version__ (API and UI banner)": match.group(1) if match else None,
        "helm-chart/Chart.yaml: version": str(chart.get("version")),
        "helm-chart/Chart.yaml: appVersion": str(chart.get("appVersion")),
        "helm-chart/values.yaml: image.tag": str(values["image"]["tag"]),
        "frontend/package.json: version": package.get("version"),
    }
    for image in images:
        if image.get("name") == "greenkube":
            versions["helm-chart/Chart.yaml: artifacthub.io/images tag"] = str(image.get("image", "")).rpartition(":")[
                2
            ]
    return versions


def check_versions(root: Path = Path(".")) -> List[str]:
    """Return one message per file whose version differs from pyproject.toml (empty when in sync)."""
    expected = get_project_version(root)
    return [
        f"{where} is {found!r}, pyproject.toml is {expected!r}"
        for where, found in read_versions(root).items()
        if found != expected
    ]


def update_readme(version):
    """Updates the version in README.md."""
    with open("README.md", "r+") as f:
        content = f.read()
        # Update "Community Edition vX.Y.Z" badge
        new_content = re.sub(r"(Community Edition v)\d+\.\d+\.\d+", rf"\g<1>{version}", content)
        # Update "Features (Version X.Y.Z)" heading
        new_content = re.sub(r"(Features \(Version )\d+\.\d+\.\d+\)", rf"\g<1>{version})", new_content)
        # Update Docker image tags in code blocks
        new_content = re.sub(r"(greenkube/greenkube:)\d+\.\d+\.\d+", rf"\g<1>{version}", new_content)
        # Update version in API health response examples
        new_content = re.sub(r'("version":")\d+\.\d+\.\d+"', rf'\g<1>{version}"', new_content)
        f.seek(0)
        f.write(new_content)
        f.truncate()


def update_uv_lock(version):
    """Updates the greenkube package version in uv.lock."""
    with open("uv.lock", "r+") as f:
        content = f.read()
        new_content = re.sub(
            r'(\[\[package\]\]\nname = "greenkube"\nversion = ")[^"]+(")',
            rf"\g<1>{version}\g<2>",
            content,
        )
        f.seek(0)
        f.write(new_content)
        f.truncate()


def update_docs(version):
    """Updates version examples in docs/api.md and docs/configuration.md."""
    with open("docs/api.md", "r+") as f:
        content = f.read()
        new_content = re.sub(r'("version":")[^"]+(")', rf"\g<1>{version}\g<2>", content)
        f.seek(0)
        f.write(new_content)
        f.truncate()

    with open("docs/configuration.md", "r+") as f:
        content = f.read()
        new_content = re.sub(r"(tag: )\d+\.\d+\.\d+", rf"\g<1>{version}", content)
        f.seek(0)
        f.write(new_content)
        f.truncate()


def update_helm_chart_yaml(version):
    """Updates the version in helm-chart/Chart.yaml, including ArtifactHub annotations."""
    yaml = YAML()
    with open("helm-chart/Chart.yaml", "r") as f:
        chart = yaml.load(f)

    chart["version"] = version
    chart["appVersion"] = version

    if "annotations" not in chart:
        chart["annotations"] = {}

    # Keep the artifacthub.io/images annotation in sync with the new version
    chart["annotations"]["artifacthub.io/images"] = (
        f"- name: greenkube\n  image: greenkube/greenkube:{version}\n"
        "  platforms:\n    - linux/amd64\n    - linux/arm64\n"
    )

    with open("helm-chart/Chart.yaml", "w") as f:
        yaml.dump(chart, f)


def update_helm_values_yaml(version):
    """Updates the version in helm-chart/values.yaml."""
    yaml = YAML()
    with open("helm-chart/values.yaml", "r") as f:
        values = yaml.load(f)

    values["image"]["tag"] = version

    with open("helm-chart/values.yaml", "w") as f:
        yaml.dump(values, f)


def update_init_py(version):
    """Updates the version in src/greenkube/__init__.py."""
    with open("src/greenkube/__init__.py", "w") as f:
        f.write(f'__version__ = "{version}"\n')


def update_demo_data_generator(version):
    """Updates the version in src/greenkube/demo/data_generator.py."""
    with open("src/greenkube/demo/data_generator.py", "r+") as f:
        content = f.read()
        new_content = re.sub(
            r'(calculation_version=")[\d]+\.[\d]+\.[\d]+(-demo")',
            rf"\g<1>{version}\g<2>",
            content,
        )
        f.seek(0)
        f.write(new_content)
        f.truncate()


def update_frontend_package_json(version):
    """Updates the version in frontend/package.json."""
    with open("frontend/package.json", "r") as f:
        data = json.load(f)

    data["version"] = version

    with open("frontend/package.json", "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def update_frontend_package_lock_json(version):
    """Updates the version in frontend/package-lock.json."""
    with open("frontend/package-lock.json", "r") as f:
        data = json.load(f)

    data["version"] = version
    if "" in data.get("packages", {}):
        data["packages"][""]["version"] = version

    with open("frontend/package-lock.json", "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main(argv: Optional[List[str]] = None) -> int:
    """Sync every file to the pyproject.toml version, or only check it with ``--check``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report diverging versions and exit 1, write nothing")
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    args = parser.parse_args(argv)
    if args.check:
        problems = check_versions(Path(args.root))
        for problem in problems:
            print(f"version mismatch: {problem}", file=sys.stderr)
        if not problems:
            print(f"All versions match pyproject.toml ({get_project_version(Path(args.root))}).")
        return 1 if problems else 0

    version = get_project_version()
    print(f"Syncing version: {version}")

    update_readme(version)
    print("Updated README.md")

    update_uv_lock(version)
    print("Updated uv.lock")

    update_docs(version)
    print("Updated docs/api.md and docs/configuration.md")

    update_helm_chart_yaml(version)
    print("Updated helm-chart/Chart.yaml")

    update_helm_values_yaml(version)
    print("Updated helm-chart/values.yaml")

    update_init_py(version)
    print("Updated src/greenkube/__init__.py")

    update_demo_data_generator(version)
    print("Updated src/greenkube/demo/data_generator.py")

    update_frontend_package_json(version)
    print("Updated frontend/package.json")

    update_frontend_package_lock_json(version)
    print("Updated frontend/package-lock.json")

    print("Version sync complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
