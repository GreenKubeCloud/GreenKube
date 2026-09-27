# tests/automation/test_patcher.py
"""Golden-file tests for the rightsizing YAML patcher."""

import pytest

from greenkube.automation.manifests.patcher import ManifestNotFoundError, RightsizingPatcher
from greenkube.models.metrics import RecommendationRecord, RecommendationType

DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments-api
  namespace: prod
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: api
          image: payments:1.2.3
          # Keep the request conservative during migration.
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: "1"
"""


def _record(**overrides) -> RecommendationRecord:
    defaults = dict(
        id=42,
        pod_name="payments-api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="CPU oversized",
        scope="workload",
        owner_kind="Deployment",
        owner_name="payments-api",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=300,
        current_memory_request_bytes=512 * 1024**2,
        patch={
            "kind": "Deployment",
            "namespace": "prod",
            "name": "payments-api",
            "operations": [{"op": "set_container_resources", "resource": "cpu", "field": "requests", "value": "300m"}],
        },
    )
    defaults.update(overrides)
    return RecommendationRecord(**defaults)


class TestRightsizingPatcher:
    def test_patches_cpu_request_and_preserves_comment(self):
        result = RightsizingPatcher().patch_content(_record(), DEPLOYMENT, path="apps/payments.yaml")

        assert result.changed
        assert "cpu: 300m" in result.patched
        assert "# Keep the request conservative during migration." in result.patched
        assert "- cpu: 500m" in result.diff.replace("-", "-", 1) or "cpu: 500m" in result.diff
        assert "cpu: 300m" in result.diff

    def test_patches_memory_and_creates_requests_when_absent(self):
        content = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: prod
spec:
  template:
    spec:
      containers:
        - name: api
          image: api:1
"""
        record = _record(
            owner_name="api",
            recommended_memory_request_bytes=256 * 1024**2,
            patch=None,
            recommended_cpu_request_millicores=None,
        )
        result = RightsizingPatcher().patch_content(record, content, path="api.yaml")

        assert "memory: 256Mi" in result.patched

    def test_missing_document_raises(self):
        record = _record(owner_name="does-not-exist")
        with pytest.raises(ManifestNotFoundError):
            RightsizingPatcher().patch_content(record, DEPLOYMENT, path="x.yaml")

    def test_multi_document_edits_only_target(self):
        content = DEPLOYMENT + "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: other\n"
        result = RightsizingPatcher().patch_content(_record(), content, path="multi.yaml")
        assert "kind: Service" in result.patched
        assert "cpu: 300m" in result.patched

    def test_long_annotation_values_are_not_wrapped(self):
        content = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: payments-api\n"
            "  namespace: prod\n"
            "  annotations:\n"
            "    greenkube.cloud/git-repo: http://gitea-http.gitea.svc.cluster.local:3000/greenkube/local-k8s.git\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: api\n"
            "          resources:\n"
            "            requests:\n"
            "              cpu: 500m\n"
        )
        result = RightsizingPatcher().patch_content(_record(), content, path="apps/payments.yaml")
        assert (
            "greenkube.cloud/git-repo: http://gitea-http.gitea.svc.cluster.local:3000/greenkube/local-k8s.git"
            in result.patched
        )
        assert "git-repo: \n" not in result.patched

    def test_find_path_locates_manifest(self):
        files = {"a.yaml": "kind: Service\nmetadata:\n  name: x\n", "b.yaml": DEPLOYMENT}
        assert RightsizingPatcher().find_path(_record(), files) == "b.yaml"

    def test_find_path_returns_none_when_absent(self):
        files = {"a.yaml": "kind: Service\nmetadata:\n  name: x\n"}
        assert RightsizingPatcher().find_path(_record(), files) is None


class TestCronJobPatcher:
    def test_patches_cronjob_containers(self):
        content = """\
apiVersion: batch/v1
kind: CronJob
metadata:
  name: report
  namespace: prod
spec:
  schedule: "0 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: report
              resources:
                requests:
                  cpu: 800m
"""
        record = _record(
            owner_kind="CronJob",
            owner_name="report",
            type=RecommendationType.RIGHTSIZING_CPU,
        )
        result = RightsizingPatcher().patch_content(record, content, path="cron.yaml")
        assert "cpu: 300m" in result.patched
