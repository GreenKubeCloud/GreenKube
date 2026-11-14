"""The self-hosted provider probe is restricted to HTTPS hosts (security hardening).

Repository hosts come from GitOps sources that anyone able to create an Argo CD
Application or a Flux source controls: the unauthenticated probe must not become
a way to make the controller call internal plain-HTTP services or the cloud
metadata endpoint.
"""

from __future__ import annotations

import pytest

from greenkube.automation import readiness
from greenkube.automation.readiness import probe_allowed, probe_provider_host


@pytest.mark.parametrize(
    ("base_url", "allowed"),
    [
        ("https://gitlab.internal.example", True),
        ("https://gitea.internal:3000", True),
        ("https://10.0.0.12", True),
        ("http://gitea.internal:3000", False),
        ("https://169.254.169.254", False),
        ("https://127.0.0.1:8443", False),
        ("https://[::1]", False),
        ("https://localhost", False),
        ("https://api.localhost", False),
        ("ssh://git.example.com", False),
        ("https://", False),
    ],
)
def test_probe_allowed(base_url, allowed):
    assert probe_allowed(base_url) is allowed


@pytest.mark.parametrize("base_url", ["http://gitea.internal:3000", "https://169.254.169.254"])
async def test_refused_probes_send_no_request(monkeypatch, base_url):
    import httpx

    def no_client(*args, **kwargs):
        raise AssertionError("the probe must not open a connection")

    monkeypatch.setattr(httpx, "AsyncClient", no_client)

    assert await probe_provider_host(base_url) is None


def test_the_readiness_service_uses_the_restricted_prober_by_default():
    service = readiness.AutomationReadiness(
        gitops_repository=object(),  # type: ignore[arg-type]
        capability_store=object(),  # type: ignore[arg-type]
        cluster="default",
        config=object(),
    )
    assert service._prober is probe_provider_host
