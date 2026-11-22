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


# --------------------------------------------------------------------------- F6 egress canary


async def test_egress_probe_reports_an_open_connection_as_unenforced():
    import asyncio

    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await service.probe_egress(("127.0.0.1", port)) is False
    finally:
        server.close()
        await server.wait_closed()


async def test_egress_probe_reports_a_refused_connection_as_enforced():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Nothing listens on that port any more: the connection fails as under a deny policy.
    assert await service.probe_egress(("127.0.0.1", port), timeout=1.0) is True
    assert await service.probe_egress(None) is None


def test_egress_probe_targets_the_cluster_dns_service(monkeypatch, tmp_path):
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("search greenkube.svc.cluster.local\nnameserver 10.96.0.10\noptions ndots:5\n")
    monkeypatch.delenv("RENDERER_EGRESS_PROBE_TARGET", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")

    # CoreDNS pods are subject to every NetworkPolicy implementation; the API server is
    # not always (kindnet allows it), so it is only the fallback.
    assert service.egress_probe_target(resolv) == ("10.96.0.10", 53)
    assert service.egress_probe_target(tmp_path / "missing") == ("10.96.0.1", 443)

    monkeypatch.setenv("RENDERER_EGRESS_PROBE_TARGET", "198.51.100.7:8443")
    assert service.egress_probe_target(resolv) == ("198.51.100.7", 8443)

    monkeypatch.delenv("RENDERER_EGRESS_PROBE_TARGET")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST")
    assert service.egress_probe_target(tmp_path / "missing") is None


def test_status_serves_the_canary_result_probed_at_startup(monkeypatch):
    calls = []

    async def probe(target, *, timeout=service.EGRESS_PROBE_TIMEOUT_SECONDS):
        calls.append(target)
        return False

    monkeypatch.setattr(service, "probe_egress", probe)
    monkeypatch.setattr(service, "EGRESS_PROBE_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setenv("RENDERER_EGRESS_PROBE_TARGET", "10.0.0.1:443")

    with TestClient(service.app) as client:
        for _ in range(50):
            body = client.get("/status").json()
            if body["egress"]["checked_at"]:
                break

    assert calls == [("10.0.0.1", 443)]
    assert body["egress"]["enforced"] is False
    assert body["egress"]["target"] == "10.0.0.1:443"
