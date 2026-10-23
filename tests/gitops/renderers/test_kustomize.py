from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from greenkube.gitops.contracts import RendererKind
from greenkube.gitops.renderers.kustomize import (
    KustomizeHelmUnsupportedError,
    KustomizeIsolationUnavailableError,
    KustomizeLimitError,
    KustomizeLimits,
    KustomizeOutputLimitError,
    KustomizePathError,
    KustomizeRemoteReferenceError,
    KustomizeRenderer,
    KustomizeTimeoutError,
    KustomizeVersionMismatchError,
)
from greenkube.gitops.renderers.raw import RawRenderResult
from greenkube.gitops.renderers.sandbox import SandboxResult, SandboxTimeoutError
from greenkube.gitops.snapshots import RepositorySnapshot, extract_archive

_RESOURCE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
data:
  mode: stable
"""


def _snapshot(tmp_path: Path, files: dict[str, str]) -> RepositorySnapshot:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w") as repository:
        for path, content in files.items():
            repository.writestr(path, content)

    return extract_archive(
        archive.getvalue(),
        provider_id="github",
        repo_url="https://example.test/org/repo",
        commit_sha="a" * 40,
        workspace_parent=tmp_path,
    )


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
        version_stdout: bytes = b"v5.4.2\n",
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
        if invocation.argv[1:] == ("version",):
            return SandboxResult(returncode=0, stdout=self.version_stdout, stderr=b"")
        return SandboxResult(
            returncode=self.build_returncode,
            stdout=self.build_stdout,
            stderr=b"",
        )


def test_kustomize_build_uses_snapshot_workspace_and_raw_render_contract(tmp_path: Path):
    snapshot = _snapshot(
        tmp_path,
        {
            "overlays/prod/kustomization.yaml": "resources:\n  - ../../base\n",
            "base/kustomization.yaml": "resources:\n  - settings.yaml\n",
            "base/settings.yaml": _RESOURCE,
        },
    )
    runner = _FakeRunner()
    renderer = KustomizeRenderer(runner=runner, expected_version="v5.4.2")

    try:
        result = renderer.render(snapshot, "overlays/prod")

        assert isinstance(result, RawRenderResult)
        assert result.renderer is RendererKind.KUSTOMIZE
        assert result.resources[0].target.name == "settings"
        assert result.resources[0].source_path == "overlays/prod/kustomization.yaml"
        assert result.resources[0].document_index == 0
        assert result.output_digest.startswith("sha256:")
        assert renderer.version_metadata is not None
        assert renderer.version_metadata.version == "v5.4.2"
        assert renderer.version_metadata.expected_version == "v5.4.2"

        assert [call.argv for call in runner.calls] == [
            ("kustomize", "version"),
            (
                "kustomize",
                "build",
                "overlays/prod",
                "--load-restrictor=LoadRestrictionsRootOnly",
            ),
        ]
        assert all(call.workspace == snapshot.workspace for call in runner.calls)
        assert all(call.cwd == snapshot.workspace for call in runner.calls)
    finally:
        snapshot.close()


@pytest.mark.parametrize(
    "remote_reference",
    [
        "https://github.com/example/repo//base?ref=main",
        "github.com/example/repo//base?ref=main",
        "git@github.com:example/repo.git",
    ],
)
def test_remote_bases_are_rejected_before_running_kustomize(tmp_path: Path, remote_reference: str):
    snapshot = _snapshot(
        tmp_path,
        {"kustomization.yaml": f"resources:\n  - {remote_reference}\n"},
    )
    runner = _FakeRunner()

    try:
        with pytest.raises(KustomizeRemoteReferenceError, match="remote"):
            KustomizeRenderer(expected_version="v5.4.2", runner=runner).render(snapshot, ".")

        assert runner.calls == []
    finally:
        snapshot.close()


def test_remote_helm_chart_repositories_are_rejected_by_default(tmp_path: Path):
    snapshot = _snapshot(
        tmp_path,
        {
            "kustomization.yaml": (
                "helmCharts:\n  - name: demo\n    repo: https://charts.example.test\n    version: 1.0.0\n"
            ),
        },
    )
    runner = _FakeRunner()

    try:
        with pytest.raises(KustomizeRemoteReferenceError, match="remote Helm charts"):
            KustomizeRenderer(expected_version="v5.4.2", runner=runner).render(snapshot, ".")

        assert runner.calls == []
    finally:
        snapshot.close()


def test_local_helm_charts_fail_closed_before_kustomize_invokes_helm(tmp_path: Path):
    snapshot = _snapshot(
        tmp_path,
        {
            "overlays/prod/kustomization.yaml": (
                "helmGlobals:\n  chartHome: ../../charts\nhelmCharts:\n  - name: demo\n    releaseName: demo\n"
            ),
            "charts/demo/Chart.yaml": "apiVersion: v2\nname: demo\nversion: 1.0.0\n",
            "charts/demo/values.yaml": "replicaCount: 1\n",
        },
    )
    runner = _FakeRunner()

    try:
        with pytest.raises(KustomizeHelmUnsupportedError, match="determinism.*schema.*dependency.*version"):
            KustomizeRenderer(expected_version="v5.4.2", runner=runner).render(snapshot, "overlays/prod")

        assert runner.calls == []
    finally:
        snapshot.close()


def test_local_components_are_recursively_checked(tmp_path: Path):
    snapshot = _snapshot(
        tmp_path,
        {
            "overlays/prod/kustomization.yaml": "components:\n  - ../../components/common\n",
            "components/common/kustomization.yaml": (
                "apiVersion: kustomize.config.k8s.io/v1alpha1\n"
                "kind: Component\n"
                "resources:\n"
                "  - ../../../../outside.yaml\n"
            ),
            "outside.yaml": _RESOURCE,
        },
    )
    runner = _FakeRunner()

    try:
        with pytest.raises(KustomizePathError, match="workspace"):
            KustomizeRenderer(expected_version="v5.4.2", runner=runner).render(snapshot, "overlays/prod")

        assert runner.calls == []
    finally:
        snapshot.close()


def test_local_components_are_included_in_the_confined_build(tmp_path: Path):
    snapshot = _snapshot(
        tmp_path,
        {
            "overlays/prod/kustomization.yaml": (
                "resources:\n  - ../../base\ncomponents:\n  - ../../components/common\n"
            ),
            "base/kustomization.yaml": "resources:\n  - settings.yaml\n",
            "base/settings.yaml": _RESOURCE,
            "components/common/kustomization.yaml": (
                "apiVersion: kustomize.config.k8s.io/v1alpha1\nkind: Component\nresources:\n  - common.yaml\n"
            ),
            "components/common/common.yaml": ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: common-settings\n"),
        },
    )
    runner = _FakeRunner()

    try:
        result = KustomizeRenderer(expected_version="v5.4.2", runner=runner).render(snapshot, "overlays/prod")

        assert result.renderer is RendererKind.KUSTOMIZE
        assert runner.calls[-1].argv[1] == "build"
    finally:
        snapshot.close()


def test_base_paths_that_escape_the_snapshot_are_rejected(tmp_path: Path):
    snapshot = _snapshot(
        tmp_path,
        {"overlays/prod/kustomization.yaml": "resources:\n  - ../../../outside\n"},
    )
    runner = _FakeRunner()

    try:
        with pytest.raises(KustomizePathError, match="workspace"):
            KustomizeRenderer(expected_version="v5.4.2", runner=runner).render(snapshot, "overlays/prod")

        assert runner.calls == []
    finally:
        snapshot.close()


def test_symlinked_bases_are_rejected(tmp_path: Path):
    snapshot = _snapshot(
        tmp_path,
        {"overlays/prod/kustomization.yaml": "resources:\n  - ../../linked-base\n"},
    )
    outside = tmp_path / "outside-base"
    outside.mkdir()
    (outside / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    (snapshot.workspace / "linked-base").symlink_to(outside, target_is_directory=True)
    runner = _FakeRunner()

    try:
        with pytest.raises(KustomizePathError, match="symbolic links"):
            KustomizeRenderer(expected_version="v5.4.2", runner=runner).render(snapshot, "overlays/prod")

        assert runner.calls == []
    finally:
        snapshot.close()


def test_kustomization_file_size_is_bounded_before_parsing(tmp_path: Path):
    snapshot = _snapshot(tmp_path, {"kustomization.yaml": "resources: []\n"})
    runner = _FakeRunner()

    try:
        limits = KustomizeLimits(max_kustomization_bytes=8)
        with pytest.raises(KustomizeLimitError, match="byte limit"):
            KustomizeRenderer(expected_version="v5.4.2", runner=runner, limits=limits).render(snapshot, ".")

        assert runner.calls == []
    finally:
        snapshot.close()


@pytest.mark.parametrize("overlay_path", ["../outside", "-production", "./-production"])
def test_overlay_path_must_be_relative_and_not_option_like(tmp_path: Path, overlay_path: str):
    snapshot = _snapshot(tmp_path, {"kustomization.yaml": "resources: []\n"})
    runner = _FakeRunner()

    try:
        with pytest.raises(KustomizePathError):
            KustomizeRenderer(expected_version="v5.4.2", runner=runner).render(snapshot, overlay_path)

        assert runner.calls == []
    finally:
        snapshot.close()


def test_default_renderer_fails_closed_without_an_isolated_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("greenkube.gitops.renderers.sandbox.shutil.which", lambda _name: None)
    snapshot = _snapshot(tmp_path, {"kustomization.yaml": "resources: [\n"})

    try:
        with pytest.raises(KustomizeIsolationUnavailableError, match="no isolated renderer backend"):
            KustomizeRenderer(expected_version="v5.4.2").render(snapshot, ".")
    finally:
        snapshot.close()


def test_version_mismatch_blocks_the_build(tmp_path: Path):
    snapshot = _snapshot(tmp_path, {"kustomization.yaml": "resources: []\n"})
    runner = _FakeRunner(version_stdout=b"v5.4.2\n")

    try:
        with pytest.raises(KustomizeVersionMismatchError, match="expected"):
            KustomizeRenderer(runner=runner, expected_version="v5.4.3").render(snapshot, ".")

        assert [call.argv for call in runner.calls] == [("kustomize", "version")]
    finally:
        snapshot.close()


def test_build_output_limit_is_enforced_even_for_an_injected_runner(tmp_path: Path):
    snapshot = _snapshot(tmp_path, {"kustomization.yaml": "resources: []\n"})
    runner = _FakeRunner(build_stdout=b"x" * 17)

    try:
        limits = KustomizeLimits(max_output_bytes=16)
        with pytest.raises(KustomizeOutputLimitError, match="output limit"):
            KustomizeRenderer(expected_version="v5.4.2", runner=runner, limits=limits).render(snapshot, ".")
    finally:
        snapshot.close()


def test_sandbox_timeout_is_exposed_as_a_typed_kustomize_error(tmp_path: Path):
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
            if tuple(argv[1:]) == ("version",):
                return super().run(
                    argv,
                    workspace=workspace,
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                    max_stdout_bytes=max_stdout_bytes,
                    max_stderr_bytes=max_stderr_bytes,
                )
            raise SandboxTimeoutError("process timed out")

    snapshot = _snapshot(tmp_path, {"kustomization.yaml": "resources: []\n"})

    try:
        with pytest.raises(KustomizeTimeoutError, match="timed out"):
            KustomizeRenderer(expected_version="v5.4.2", runner=_TimeoutRunner()).render(snapshot, ".")
    finally:
        snapshot.close()
