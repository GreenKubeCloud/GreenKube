from __future__ import annotations

import base64
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from greenkube.gitops.renderers import service
from greenkube.gitops.renderers.sandbox import SandboxResult


def _archive() -> bytes:
    stream = io.BytesIO()
    content = b"apiVersion: v2\nname: demo\nversion: 0.1.0\n"
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        entry = tarfile.TarInfo("charts/demo/Chart.yaml")
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    return stream.getvalue()


def _metadata(argv: list[str]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(
            {
                "argv": argv,
                "cwd": ".",
                "timeout_seconds": 30,
                "max_stdout_bytes": 1_024,
                "max_stderr_bytes": 1_024,
            }
        ).encode("utf-8")
    ).decode("ascii")


def test_renderer_service_extracts_bounded_snapshot_and_executes_only_pinned_tools(monkeypatch):
    calls: list[tuple[tuple[str, ...], dict]] = []

    def fake_run(self, argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return SandboxResult(returncode=0, stdout=b"rendered", stderr=b"", duration_seconds=0.1)

    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.BoundedSubprocessRunner, "run", fake_run)

    with TestClient(service.app) as client:
        response = client.post(
            "/render",
            content=_archive(),
            headers={
                "X-GK-Render-Request": _metadata(["helm", "template", "demo", "__GREENCUBE_WORKSPACE__/charts/demo"])
            },
        )

    assert response.status_code == 200
    assert base64.b64decode(response.json()["stdout"]) == b"rendered"
    argv, kwargs = calls[0]
    assert argv[0] == "/usr/bin/prlimit"
    assert "--" in argv
    assert argv[-4:-1] == ("/usr/bin/helm", "template", "demo")
    assert argv[-1] == str((kwargs["workspace"] / "charts/demo").resolve())


@pytest.mark.parametrize("argv", (["sh", "-c", "id"], ["helm", "template", "demo", "/etc/passwd"]))
def test_renderer_service_rejects_unsupported_commands_and_paths(argv):
    with TestClient(service.app) as client:
        response = client.post(
            "/render",
            content=_archive(),
            headers={"X-GK-Render-Request": _metadata(argv)},
        )

    assert response.status_code == 400
