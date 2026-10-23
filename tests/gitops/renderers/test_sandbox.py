from __future__ import annotations

import base64
import io
import json
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

from greenkube.gitops.renderers.sandbox import (
    BoundedSubprocessRunner,
    SandboxIsolationUnavailableError,
    SandboxOutputLimitError,
    SandboxPathError,
    SandboxRunner,
    SandboxTimeoutError,
    _sandbox_command,
    _workspace_archive,
)


def test_default_runner_fails_closed_without_an_isolated_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    monkeypatch.setattr("greenkube.gitops.renderers.sandbox.shutil.which", lambda _name: None)

    def unexpected_process(*args, **kwargs):
        pytest.fail("the default renderer runner must not start a local subprocess")

    monkeypatch.setattr("greenkube.gitops.renderers.sandbox.subprocess.Popen", unexpected_process)

    with pytest.raises(SandboxIsolationUnavailableError, match="no isolated renderer backend"):
        SandboxRunner().run(
            ["helm", "version"],
            workspace=workspace,
            cwd=workspace,
            timeout_seconds=1,
            max_stdout_bytes=1_024,
            max_stderr_bytes=1_024,
        )


def test_sandbox_command_isolates_network_secrets_and_snapshot(tmp_path: Path):
    workspace = tmp_path / "snapshot"
    workspace.mkdir()

    command = _sandbox_command(
        (
            "/usr/local/bin/helm",
            "template",
            "demo",
            str(workspace / "charts/demo"),
            "--values",
            str(workspace / "values.yaml"),
        ),
        workspace=workspace,
        cwd=workspace,
        bubblewrap_binary="/usr/bin/bwrap",
        prlimit_binary="/usr/bin/prlimit",
        timeout_seconds=30,
    )

    assert "--unshare-net" in command
    assert "--unshare-pid" in command
    assert "--ro-bind" in command
    assert "/greenkube-render-workspace/charts/demo" in command
    assert "/greenkube-render-workspace/values.yaml" in command
    assert "/etc/greenkube" in command
    assert "/run" in command
    assert "--as=4294967296" in command
    assert "--nproc=64" in command
    bwrap_separator = command.index("--")
    assert str(workspace) not in command[bwrap_separator + 1 :]


def test_sandbox_rejects_absolute_paths_outside_snapshot(tmp_path: Path):
    workspace = tmp_path / "snapshot"
    workspace.mkdir()

    with pytest.raises(SandboxPathError, match="inside the snapshot workspace"):
        _sandbox_command(
            ("/usr/local/bin/helm", "template", "demo", "/etc/passwd"),
            workspace=workspace,
            cwd=workspace,
            bubblewrap_binary="/usr/bin/bwrap",
            prlimit_binary="/usr/bin/prlimit",
            timeout_seconds=30,
        )


def test_remote_runner_sends_bounded_snapshot_and_uses_the_isolated_service(tmp_path: Path):
    workspace = tmp_path / "snapshot"
    chart = workspace / "charts/demo"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: demo\nversion: 0.1.0\n", encoding="utf-8")
    (workspace / "values.yaml").write_text("replicas: 1\n", encoding="utf-8")
    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        metadata = json.loads(base64.urlsafe_b64decode(request.headers["X-GK-Render-Request"]))
        observed["metadata"] = metadata
        archive_data = request.read()
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
            observed["files"] = sorted(archive.getnames())
        return httpx.Response(
            200,
            json={
                "returncode": 0,
                "stdout": base64.b64encode(b"rendered").decode("ascii"),
                "stderr": "",
                "duration_seconds": 0.2,
            },
        )

    runner = SandboxRunner(
        renderer_url="http://greenkube-renderer:8090",
        http_transport=httpx.MockTransport(handle),
    )
    result = runner.run(
        [
            "/usr/local/bin/helm",
            "template",
            "demo",
            str(chart),
            "--values",
            str(workspace / "values.yaml"),
        ],
        workspace=workspace,
        cwd=workspace,
        timeout_seconds=30,
        max_stdout_bytes=1_024,
        max_stderr_bytes=1_024,
    )

    assert result.stdout == b"rendered"
    assert observed["url"] == "http://greenkube-renderer:8090/render"
    assert observed["files"] == ["charts/demo/Chart.yaml", "values.yaml"]
    metadata = observed["metadata"]
    assert metadata["argv"] == [  # type: ignore[index]
        "helm",
        "template",
        "demo",
        "__GREENCUBE_WORKSPACE__/charts/demo",
        "--values",
        "__GREENCUBE_WORKSPACE__/values.yaml",
    ]
    assert metadata["cwd"] == "."  # type: ignore[index]


def test_remote_snapshot_rejects_symlinks(tmp_path: Path):
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    (workspace / "link").symlink_to(tmp_path)

    with pytest.raises(SandboxPathError, match="symbolic links"):
        with _workspace_archive(workspace):
            pytest.fail("unsafe workspace should not be transmitted")


def test_bounded_local_runner_scrubs_credentials_and_uses_workspace_for_home_and_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    monkeypatch.setenv("KUBECONFIG", "/credentials/kubeconfig")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "cloud-secret")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/credentials/gcp.json")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "cloud-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "git-secret")
    monkeypatch.setenv("GIT_ASKPASS", "/credentials/git-helper")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/credentials/agent.sock")
    script = (
        "import os; "
        "keys = ('KUBECONFIG', 'AWS_ACCESS_KEY_ID', 'GOOGLE_APPLICATION_CREDENTIALS', "
        "'AZURE_CLIENT_SECRET', 'GITHUB_TOKEN', 'GIT_ASKPASS', 'SSH_AUTH_SOCK'); "
        "print('|'.join(os.getenv(key, '<unset>') for key in keys)); "
        "print(os.environ['HOME']); "
        "print(os.environ['TMPDIR'])"
    )

    result = BoundedSubprocessRunner().run(
        [sys.executable, "-c", script],
        workspace=workspace,
        cwd=workspace,
        timeout_seconds=2,
        max_stdout_bytes=2_048,
        max_stderr_bytes=1_024,
    )

    secret_vars, home, temp_dir = result.stdout.decode().splitlines()
    assert secret_vars == "|".join(["<unset>"] * 7)
    assert Path(home).is_relative_to(workspace)
    assert Path(temp_dir).is_relative_to(workspace)
    assert result.returncode == 0


def test_bounded_local_runner_rejects_a_working_directory_outside_the_snapshot(tmp_path: Path):
    workspace = tmp_path / "snapshot"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    with pytest.raises(SandboxPathError, match="workspace"):
        BoundedSubprocessRunner().run(
            [sys.executable, "-c", "pass"],
            workspace=workspace,
            cwd=outside,
            timeout_seconds=1,
            max_stdout_bytes=1_024,
            max_stderr_bytes=1_024,
        )


def test_bounded_local_runner_enforces_stdout_limits_while_the_process_runs(tmp_path: Path):
    workspace = tmp_path / "snapshot"
    workspace.mkdir()

    with pytest.raises(SandboxOutputLimitError, match="stdout"):
        BoundedSubprocessRunner().run(
            [sys.executable, "-c", "print('x' * 1000)"],
            workspace=workspace,
            timeout_seconds=2,
            max_stdout_bytes=16,
            max_stderr_bytes=1_024,
        )


def test_bounded_local_runner_terminates_timed_out_processes(tmp_path: Path):
    workspace = tmp_path / "snapshot"
    workspace.mkdir()

    with pytest.raises(SandboxTimeoutError, match="timed out"):
        BoundedSubprocessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            workspace=workspace,
            timeout_seconds=0.1,
            max_stdout_bytes=1_024,
            max_stderr_bytes=1_024,
        )
