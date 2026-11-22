"""Values schema of the production-readiness keys (scheduling, backup, operations alerts)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

CHART_PATH = Path("helm-chart")

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _template(*values: str) -> subprocess.CompletedProcess[str]:
    args = [arg for value in values for arg in ("--set", value)]
    return subprocess.run(
        ["helm", "template", "greenkube", str(CHART_PATH), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (["api.replicas=2", "api.podAntiAffinity=maybe"], "podAntiAffinity"),
        (["controller.strategy.type=BlueGreen"], "strategy/type"),
        (["api.podDisruptionBudget.maxUnavailable=1"], "podDisruptionBudget"),
        (["postgres.image.tag=17-alpine", "postgres.image.digest=latest"], "digest"),
        (["initContainer.image.digest=sha256:nope"], "digest"),
        (["postgres.backup.keep=0"], "keep"),
        (["postgres.backup.persistence.existingClaim=Not_A_Name"], "existingClaim"),
        (["monitoring.prometheusRule.operations.volumeFullWithinHours=0"], "volumeFullWithinHours"),
        (["monitoring.prometheusRule.operations.unknown=true"], "operations"),
        (["global.imagePullSecrets[0].secret=x"], "imagePullSecrets"),
    ],
)
def test_schema_rejects_invalid_production_values(values: list[str], message: str):
    result = _template(*values)

    assert result.returncode != 0
    assert message in result.stderr


def test_schema_accepts_a_production_values_set():
    result = _template(
        "api.replicas=2",
        "controller.replicas=2",
        "api.podAntiAffinity=hard",
        "api.nodeSelector.pool=system",
        "api.podDisruptionBudget.minAvailable=50%",
        "controller.strategy.type=RollingUpdate",
        "controller.strategy.rollingUpdate.maxSurge=0",
        "global.imagePullSecrets[0]=regcred",
        "postgres.backup.enabled=true",
        "postgres.backup.keep=30",
        "postgres.shmSizeLimit=256Mi",
        "monitoring.prometheusRule.enabled=true",
        "monitoring.prometheusRule.operations.backupMaxAgeHours=50",
    )

    assert result.returncode == 0, result.stderr
