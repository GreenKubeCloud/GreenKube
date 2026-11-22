"""The packaged chart ships what installs need and nothing else (roadmap N2/O6 hygiene)."""

import subprocess
import tarfile
from pathlib import Path

CHART_PATH = Path("helm-chart")


def test_package_excludes_unit_tests_but_keeps_runtime_files(tmp_path):
    subprocess.run(
        ["helm", "package", str(CHART_PATH), "--destination", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    (archive,) = tmp_path.glob("greenkube-*.tgz")
    with tarfile.open(archive) as package:
        names = set(package.getnames())

    assert not [name for name in names if name.startswith("greenkube/tests/")]
    assert "greenkube/templates/tests/test-connection.yaml" in names
    assert "greenkube/files/credentials_bootstrap.py" in names
    assert "greenkube/values.schema.json" in names
    # One install topology (F11): no profile values file any more.
    assert "greenkube/values-production.yaml" not in names
