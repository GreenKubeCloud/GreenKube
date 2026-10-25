from __future__ import annotations

import io
import shutil
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from greenkube.gitops.contracts import RendererKind
from greenkube.gitops.renderers.helm import (
    HelmClusterDependentError,
    HelmIsolationUnavailableError,
    HelmLimits,
    HelmOutputError,
    HelmOutputLimitError,
    HelmPathError,
    HelmRemoteReferenceError,
    HelmRenderer,
    HelmTemplateError,
    HelmTimeoutError,
    HelmUnvalidatedError,
    HelmVersionMismatchError,
)
from greenkube.gitops.renderers.raw import RawRenderResult
from greenkube.gitops.renderers.sandbox import BoundedSubprocessRunner, SandboxResult, SandboxTimeoutError
from greenkube.gitops.snapshots import RepositorySnapshot, extract_archive

_VERSION = "v3.18.6"
_RESOURCE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: prod
data:
  mode: stable
"""


def _snapshot(files: dict[str, str]) -> RepositorySnapshot:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w") as repository:
        for path, content in files.items():
            repository.writestr(path, content)

    return extract_archive(
        archive.getvalue(),
        provider_id="github",
        repo_url="https://example.test/org/repo",
        commit_sha="a" * 40,
        workspace_parent=Path.cwd(),
    )


def _chart_files(*, template: str = "") -> dict[str, str]:
    return {
        "charts/demo/Chart.yaml": "apiVersion: v2\nname: demo\nversion: 1.0.0\n",
        "charts/demo/templates/configmap.yaml": template or "{{- /* static chart */ -}}\n",
        "config/values-base.yaml": "mode: base\n",
        "config/values-prod.yaml": "mode: prod\n",
    }


def test_helm_renderer_uses_chart_supplied_cluster_capabilities(monkeypatch):
    monkeypatch.setenv("GITOPS_HELM_KUBE_VERSION", "v1.32.1")
    monkeypatch.setenv(
        "GITOPS_HELM_API_VERSIONS_JSON",
        '["apps/v1","monitoring.coreos.com/v1"]',
    )

    renderer = HelmRenderer(expected_version=_VERSION, runner=_FakeRunner())

    assert renderer.kube_version == "1.32.1"
    assert renderer.api_versions == ("apps/v1", "monitoring.coreos.com/v1")


def test_helm_renderer_rejects_invalid_chart_supplied_api_versions(monkeypatch):
    monkeypatch.setenv("GITOPS_HELM_API_VERSIONS_JSON", '{"apps/v1":true}')

    with pytest.raises(ValueError, match="must be a JSON array"):
        HelmRenderer(expected_version=_VERSION, runner=_FakeRunner())


@dataclass(frozen=True, slots=True)
class _Invocation:
    argv: tuple[str, ...]
    workspace: Path
    cwd: Path
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int


class _FakeRunner:
    def __init__(
        self,
        *,
        build_stdout: bytes = _RESOURCE.encode(),
        version_stdout: bytes = f"{_VERSION}\n".encode(),
        build_returncode: int = 0,
    ) -> None:
        self.build_stdout = build_stdout
        self.version_stdout = version_stdout
        self.build_returncode = build_returncode
        self.calls: list[_Invocation] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> SandboxResult:
        invocation = _Invocation(
            tuple(argv),
            workspace,
            cwd,
            timeout_seconds,
            max_stdout_bytes,
            max_stderr_bytes,
        )
        self.calls.append(invocation)
        if invocation.argv[1:] == ("version", "--short"):
            return SandboxResult(returncode=0, stdout=self.version_stdout, stderr=b"")
        return SandboxResult(
            returncode=self.build_returncode,
            stdout=self.build_stdout,
            stderr=b"template diagnostic",
        )


def test_helm_template_uses_snapshot_ordered_values_and_deterministic_capabilities():
    snapshot = _snapshot(_chart_files())
    runner = _FakeRunner()
    renderer = HelmRenderer(
        expected_version=_VERSION,
        runner=runner,
        kube_version="1.31.2",
        api_versions=("monitoring.coreos.com/v1", "apps/v1"),
        limits=HelmLimits(timeout_seconds=7.5, max_output_bytes=8_192, max_stderr_bytes=1_024),
    )

    try:
        result = renderer.render(
            snapshot,
            "charts/demo",
            release_name="demo-release",
            release_namespace="prod",
            values_files=("config/values-base.yaml", "config/values-prod.yaml"),
        )

        assert isinstance(result, RawRenderResult)
        assert result.renderer is RendererKind.HELM_RELEASE
        assert result.resources[0].target.name == "settings"
        assert result.resources[0].target.namespace == "prod"
        assert result.resources[0].source_path == "charts/demo/Chart.yaml"
        assert result.resources[0].document_index == 0
        assert result.output_digest.startswith("sha256:")
        assert result.warnings == ()
        assert renderer.version_metadata is not None
        assert renderer.version_metadata.version == _VERSION
        assert renderer.version_metadata.expected_version == _VERSION
        assert [call.argv for call in runner.calls] == [
            ("helm", "version", "--short"),
            (
                "helm",
                "template",
                "demo-release",
                "charts/demo",
                "--namespace",
                "prod",
                "--kube-version",
                "1.31.2",
                "--no-hooks",
                "--api-versions",
                "apps/v1",
                "--api-versions",
                "monitoring.coreos.com/v1",
                "--values",
                "config/values-base.yaml",
                "--values",
                "config/values-prod.yaml",
            ),
        ]
        assert all(call.workspace == snapshot.workspace for call in runner.calls)
        assert all(call.cwd == snapshot.workspace for call in runner.calls)
        assert all(call.timeout_seconds == 7.5 for call in runner.calls)
        assert all(call.max_stdout_bytes == 8_192 for call in runner.calls)
        assert all(call.max_stderr_bytes == 1_024 for call in runner.calls)
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    ("chart_path", "values_files", "error"),
    [
        ("-charts/demo", (), HelmPathError),
        ("./-charts/demo", (), HelmPathError),
        ("/charts/demo", (), HelmPathError),
        ("../charts/demo", (), HelmPathError),
        ("charts/../demo", (), HelmPathError),
        ("charts/demo", ("-config/values.yaml",), HelmPathError),
        ("charts/demo", ("./-config/values.yaml",), HelmPathError),
        ("charts/demo", ("/config/values.yaml",), HelmPathError),
        ("charts/demo", ("../config/values.yaml",), HelmPathError),
        ("charts/demo", ("config/../values.yaml",), HelmPathError),
    ],
)
def test_option_like_absolute_and_traversing_chart_or_values_paths_are_rejected(
    chart_path: str,
    values_files: tuple[str, ...],
    error: type[Exception],
):
    snapshot = _snapshot(_chart_files())
    runner = _FakeRunner()

    try:
        with pytest.raises(error):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                chart_path,
                release_name="demo",
                namespace="prod",
                values_files=values_files,
            )

        assert runner.calls == []
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "reference",
    [
        "https://charts.example.test/demo",
        "oci://registry.example.test/charts/demo",
        "charts.example.test/demo",
        "git@github.com:example/demo.git",
    ],
)
def test_remote_chart_references_are_rejected(reference: str):
    snapshot = _snapshot(_chart_files())
    runner = _FakeRunner()

    try:
        with pytest.raises(HelmRemoteReferenceError, match="remote"):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                reference,
                release_name="demo",
                namespace="prod",
            )

        assert runner.calls == []
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "reference",
    [
        "https://charts.example.test/values.yaml",
        "oci://registry.example.test/values",
        "charts.example.test/values.yaml",
        "file://config/values.yaml",
    ],
)
def test_remote_values_references_are_rejected(reference: str):
    snapshot = _snapshot(_chart_files())
    runner = _FakeRunner()

    try:
        with pytest.raises(HelmRemoteReferenceError, match="remote"):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
                values_files=(reference,),
            )

        assert runner.calls == []
    finally:
        snapshot.close()


def test_remote_chart_dependencies_are_rejected_before_running_helm():
    files = _chart_files()
    files["charts/demo/Chart.yaml"] = (
        "apiVersion: v2\n"
        "name: demo\n"
        "version: 1.0.0\n"
        "dependencies:\n"
        "  - name: common\n"
        "    version: 1.2.3\n"
        "    repository: https://charts.example.test\n"
    )
    snapshot = _snapshot(files)
    runner = _FakeRunner()

    try:
        with pytest.raises(HelmRemoteReferenceError, match="remote"):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )

        assert runner.calls == []
    finally:
        snapshot.close()


def test_local_chart_dependencies_are_resolved_inside_the_chart_directory():
    files = _chart_files()
    files["charts/demo/Chart.yaml"] = (
        "apiVersion: v2\n"
        "name: demo\n"
        "version: 1.0.0\n"
        "dependencies:\n"
        "  - name: common\n"
        "    version: 1.0.0\n"
        "    repository: file://charts/common\n"
    )
    files["charts/demo/charts/common/Chart.yaml"] = "apiVersion: v2\nname: common\nversion: 1.0.0\n"
    snapshot = _snapshot(files)
    runner = _FakeRunner()

    try:
        result = HelmRenderer(expected_version=_VERSION, runner=runner).render(
            snapshot,
            "charts/demo",
            release_name="demo",
            namespace="prod",
        )

        assert result.renderer is RendererKind.HELM_RELEASE
        assert len(runner.calls) == 2
    finally:
        snapshot.close()


def test_traversing_local_chart_dependencies_are_rejected():
    files = _chart_files()
    files["charts/demo/Chart.yaml"] = (
        "apiVersion: v2\n"
        "name: demo\n"
        "version: 1.0.0\n"
        "dependencies:\n"
        "  - name: common\n"
        "    version: 1.0.0\n"
        "    repository: file://../common\n"
    )
    snapshot = _snapshot(files)
    runner = _FakeRunner()

    try:
        with pytest.raises(HelmPathError, match="traverse"):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )

        assert runner.calls == []
    finally:
        snapshot.close()


def test_remote_chart_lock_dependencies_are_rejected():
    files = _chart_files()
    files["charts/demo/Chart.lock"] = (
        "dependencies:\n  - name: common\n    version: 1.0.0\n    repository: https://charts.example.test\n"
    )
    snapshot = _snapshot(files)
    runner = _FakeRunner()

    try:
        with pytest.raises(HelmRemoteReferenceError, match="remote"):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )

        assert runner.calls == []
    finally:
        snapshot.close()


def test_packaged_chart_dependencies_are_rejected_when_their_templates_cannot_be_validated():
    files = _chart_files()
    files["charts/demo/charts/common-1.0.0.tgz"] = "opaque chart archive"
    snapshot = _snapshot(files)
    runner = _FakeRunner()

    try:
        with pytest.raises(HelmUnvalidatedError, match="cannot be inspected"):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )

        assert runner.calls == []
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    ("schema", "error"),
    [
        ('{"$ref": "https://schemas.example.test/base.json"}', HelmRemoteReferenceError),
        ('{"$ref": "../shared.schema.json"}', HelmPathError),
    ],
)
def test_remote_or_traversing_values_schema_dependencies_are_rejected(schema: str, error: type[Exception]):
    files = _chart_files()
    files["charts/demo/values.schema.json"] = schema
    snapshot = _snapshot(files)
    runner = _FakeRunner()

    try:
        with pytest.raises(error):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )

        assert runner.calls == []
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    ("function", "error"),
    [
        ('lookup "v1" "ConfigMap" "prod" "settings"', HelmClusterDependentError),
        ("randAlphaNum 8", HelmClusterDependentError),
        ('now | date "2006"', HelmClusterDependentError),
    ],
)
def test_cluster_dependent_or_nondeterministic_templates_are_rejected_before_running_helm(
    function: str,
    error: type[Exception],
):
    snapshot = _snapshot(_chart_files(template=f"{{{{ {function} }}}}\n"))
    runner = _FakeRunner()

    try:
        with pytest.raises(error):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )

        assert runner.calls == []
    finally:
        snapshot.close()


def test_lookup_in_a_go_template_comment_is_not_treated_as_cluster_dependent():
    snapshot = _snapshot(_chart_files(template="{{/* lookup is documented here, but not executed */}}\n" + _RESOURCE))
    runner = _FakeRunner()

    try:
        result = HelmRenderer(expected_version=_VERSION, runner=runner).render(
            snapshot,
            "charts/demo",
            release_name="demo",
            namespace="prod",
        )

        assert result.renderer is RendererKind.HELM_RELEASE
        assert len(runner.calls) == 2
    finally:
        snapshot.close()


def test_helm_source_comments_map_resources_to_in_snapshot_templates():
    output = f"---\n# Source: demo/templates/configmap.yaml\n{_RESOURCE}".encode()
    snapshot = _snapshot(_chart_files())
    runner = _FakeRunner(build_stdout=output)

    try:
        result = HelmRenderer(expected_version=_VERSION, runner=runner).render(
            snapshot,
            "charts/demo",
            release_name="demo",
            namespace="prod",
        )

        assert result.resources[0].source_path == "charts/demo/templates/configmap.yaml"
    finally:
        snapshot.close()


def test_template_errors_do_not_expose_stderr_and_raw_parse_errors_are_typed():
    snapshot = _snapshot(_chart_files())

    try:
        failing_runner = _FakeRunner(build_returncode=1)
        with pytest.raises(HelmTemplateError, match="exit status 1") as failure:
            HelmRenderer(expected_version=_VERSION, runner=failing_runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )
        assert "template diagnostic" not in str(failure.value)

        invalid_runner = _FakeRunner(build_stdout=b"apiVersion: v1\nkind: ConfigMap\n")
        with pytest.raises(HelmOutputError, match="valid bounded Kubernetes"):
            HelmRenderer(expected_version=_VERSION, runner=invalid_runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )
    finally:
        snapshot.close()


def test_version_mismatch_is_rejected_before_template_execution():
    snapshot = _snapshot(_chart_files())
    runner = _FakeRunner(version_stdout=b"v3.17.0\n")

    try:
        with pytest.raises(HelmVersionMismatchError, match="does not match"):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )

        assert [call.argv[1:] for call in runner.calls] == [("version", "--short")]
    finally:
        snapshot.close()


def test_sandbox_timeouts_are_mapped_to_helm_timeouts():
    class _TimeoutRunner(_FakeRunner):
        def run(
            self,
            argv: Sequence[str],
            *,
            workspace: Path,
            cwd: Path,
            timeout_seconds: float,
            max_stdout_bytes: int,
            max_stderr_bytes: int,
        ) -> SandboxResult:
            if tuple(argv)[1:] == ("version", "--short"):
                return SandboxResult(returncode=0, stdout=f"{_VERSION}\n".encode(), stderr=b"")
            raise SandboxTimeoutError("sandbox timed out")

    snapshot = _snapshot(_chart_files())

    try:
        with pytest.raises(HelmTimeoutError, match="timed out"):
            HelmRenderer(expected_version=_VERSION, runner=_TimeoutRunner()).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )
    finally:
        snapshot.close()


def test_output_limit_is_enforced_even_when_an_injected_runner_ignores_it():
    snapshot = _snapshot(_chart_files())
    runner = _FakeRunner(build_stdout=b"x" * 32)

    try:
        with pytest.raises(HelmOutputLimitError, match="stdout output limit"):
            HelmRenderer(
                expected_version=_VERSION,
                runner=runner,
                limits=HelmLimits(max_output_bytes=16),
            ).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )
    finally:
        snapshot.close()


def test_default_renderer_fails_closed_without_an_isolated_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("greenkube.gitops.renderers.sandbox.shutil.which", lambda _name: None)
    snapshot = _snapshot({"charts/demo/Chart.yaml": "invalid: [\n"})

    try:
        with pytest.raises(HelmIsolationUnavailableError, match="no isolated renderer backend"):
            HelmRenderer(expected_version=_VERSION).render(
                snapshot,
                "charts/demo",
                release_name="demo",
                namespace="prod",
            )
    finally:
        snapshot.close()


def test_bounded_local_runner_scrubs_credentials_for_helm_process(monkeypatch: pytest.MonkeyPatch):
    snapshot = _snapshot(_chart_files())
    binary = snapshot.workspace / "fake-helm"
    binary.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "version" ]; then printf "%s\\n" "v3.18.6"; exit 0; fi\n'
        'if [ -n "$KUBECONFIG$AWS_ACCESS_KEY_ID$GITHUB_TOKEN" ]; then exit 29; fi\n'
        f"cat <<'MANIFEST'\n{_RESOURCE}MANIFEST\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setenv("KUBECONFIG", "/credentials/kubeconfig")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "cloud-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "git-secret")

    try:
        result = HelmRenderer(
            expected_version=_VERSION,
            binary=str(binary),
            runner=BoundedSubprocessRunner(),
        ).render(
            snapshot,
            "charts/demo",
            release_name="demo",
            namespace="prod",
        )

        assert result.renderer is RendererKind.HELM_RELEASE
        assert result.resources[0].target.name == "settings"
    finally:
        snapshot.close()


def test_rejects_symlinked_chart_paths_before_running_helm():
    snapshot = _snapshot(_chart_files())
    chart_link = snapshot.workspace / "charts" / "linked"
    chart_link.symlink_to(snapshot.workspace / "charts" / "demo", target_is_directory=True)
    runner = _FakeRunner()

    try:
        with pytest.raises(HelmPathError, match="symbolic link"):
            HelmRenderer(expected_version=_VERSION, runner=runner).render(
                snapshot,
                "charts/linked",
                release_name="demo",
                namespace="prod",
            )

        assert runner.calls == []
    finally:
        snapshot.close()


def test_real_helm_renders_a_snapshot_chart_when_the_binary_is_available():
    binary = shutil.which("helm")
    if binary is None:
        pytest.skip("Helm binary is unavailable; injected-runner tests cover rendering behavior")

    template = """\
{{- if .Capabilities.APIVersions.Has "example.test/v1" }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: {{ .Release.Namespace }}
data:
  mode: {{ .Values.mode | quote }}
{{- end }}
"""
    snapshot = _snapshot(_chart_files(template=template))
    runner = BoundedSubprocessRunner()

    try:
        version_result = runner.run(
            [binary, "version", "--short"],
            workspace=snapshot.workspace,
            cwd=snapshot.workspace,
            timeout_seconds=30,
            max_stdout_bytes=1_024,
            max_stderr_bytes=1_024,
        )
        expected_version = version_result.stdout.decode("utf-8").strip()
        result = HelmRenderer(expected_version=expected_version, binary=binary, runner=runner).render(
            snapshot,
            "charts/demo",
            release_name="demo-release",
            namespace="prod",
            values_files=("config/values-base.yaml", "config/values-prod.yaml"),
            api_versions=("example.test/v1",),
        )

        assert result.renderer is RendererKind.HELM_RELEASE
        assert result.resources[0].target.name == "settings"
        assert result.resources[0].source_path == "charts/demo/templates/configmap.yaml"
        assert result.resources[0].manifest["data"]["mode"] == "prod"
        assert result.resources[0].manifest["metadata"]["namespace"] == "prod"
    finally:
        snapshot.close()
