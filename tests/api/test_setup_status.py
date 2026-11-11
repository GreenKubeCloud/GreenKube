"""Guided setup checklist (F1) and allowlist suggestions (F2): the shared contract with the UI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from greenkube.api.routers import automation_repositories, setup
from greenkube.models.health import ServiceHealth, ServiceStatus
from greenkube.storage.automation_capability_repository import CapabilityRow

CATEGORIES = {
    "data_sources",
    "recommendation_sources",
    "gitops",
    "repositories",
    "credentials",
    "renderer",
    "executor",
    "evidence",
}
STATUSES = {"ok", "action_needed", "blocked", "not_applicable", "pending"}
REPO = "https://github.com/acme/gitops.git"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _globals(**states: tuple[str, str | None]) -> list[CapabilityRow]:
    defaults: dict[str, tuple[str, str | None]] = {
        "discovery": ("active", None),
        "preview": ("active", None),
        "delivery": ("active", None),
        "sync_tracking": ("active", None),
        "webhooks": ("dormant", "webhook_secret_missing"),
        "measurement": ("active", None),
        "renderer": ("active", None),
    }
    defaults.update(states)
    return [
        CapabilityRow("global", name, state, {"reason": reason, "controllers": ["argocd"]}, _now())
        for name, (state, reason) in defaults.items()
    ]


def _repo_row(allowlisted: bool = True, provider_type: str | None = "github") -> CapabilityRow:
    return CapabilityRow(
        "repository",
        REPO,
        "active" if allowlisted else "dormant",
        {
            "repository": "acme/gitops",
            "provider_type": provider_type,
            "api_base_url": None,
            "inferred_from": "host",
            "allowlisted": allowlisted,
        },
        _now(),
    )


@pytest.fixture
def inputs(monkeypatch):
    state = SimpleNamespace(
        services={
            "prometheus": ServiceHealth(name="prometheus", status=ServiceStatus.HEALTHY),
            "opencost": ServiceHealth(name="opencost", status=ServiceStatus.UNREACHABLE),
            "electricity_maps": ServiceHealth(name="electricity_maps", status=ServiceStatus.UNCONFIGURED),
        },
        sources=[{"id": "greenkube.rightsizing", "status": "healthy", "enabled": "auto"}],
        rows=[*_globals(), _repo_row()],
        heartbeat=_now(),
        earliest=_now() - timedelta(days=30),
    )

    async def services():
        return state.services

    async def sources():
        return state.sources

    async def rows():
        return state.rows

    async def heartbeat():
        return state.heartbeat

    async def earliest():
        return state.earliest

    monkeypatch.setattr(setup, "_health_services", services)
    monkeypatch.setattr(setup, "_source_statuses", sources)
    monkeypatch.setattr(setup, "_capabilities", rows)
    monkeypatch.setattr(setup, "_executor_heartbeat", heartbeat)
    monkeypatch.setattr(setup, "_earliest_sample", earliest)
    return state


def _checks(body: dict) -> dict[str, dict]:
    return {check["id"]: check for check in body["checks"]}


def test_contract_shape(client, inputs):
    body = client.get("/api/v1/setup/status").json()

    assert set(body) == {"complete", "checks", "capabilities"}
    assert set(body["capabilities"]) == {"discovery", "delivery", "sync_tracking", "measurement"}
    for capability in body["capabilities"].values():
        assert set(capability) == {"state", "reason"}
    for check in body["checks"]:
        assert set(check) == {"id", "category", "title", "status", "reason", "fix", "docs_url"}
        assert check["category"] in CATEGORIES
        assert check["status"] in STATUSES
        assert set(check["fix"]) == {"text", "snippet", "ui_action"}
        assert check["docs_url"].startswith("https://")
    assert {check["category"] for check in body["checks"]} == CATEGORIES


def test_complete_when_everything_is_ok_or_optional(client, inputs):
    body = client.get("/api/v1/setup/status").json()

    assert body["complete"] is True
    checks = _checks(body)
    assert checks["opencost"]["status"] == "not_applicable"
    assert checks["git_credentials"]["status"] == "ok"
    assert body["capabilities"]["delivery"] == {"state": "active", "reason": None}


def test_missing_credentials_offer_the_one_secret_fix(client, inputs):
    inputs.rows = [*_globals(delivery=("dormant", "credentials_missing")), _repo_row()]
    inputs.heartbeat = None

    body = client.get("/api/v1/setup/status").json()

    check = _checks(body)["git_credentials"]
    assert body["complete"] is False
    assert check["status"] == "action_needed"
    assert "create secret generic greenkube-git-credentials" in check["fix"]["snippet"]
    assert body["capabilities"]["delivery"] == {"state": "dormant", "reason": "credentials_missing"}
    # The executor is not expected before delivery activates.
    assert _checks(body)["executor"]["status"] == "not_applicable"


def test_paused_delivery_is_blocked_by_policy(client, inputs):
    inputs.rows = [*_globals(delivery=("dormant", "delivery_paused")), _repo_row()]

    check = _checks(client.get("/api/v1/setup/status").json())["git_credentials"]

    assert check["status"] == "blocked"
    assert "pauseDelivery=false" in check["fix"]["snippet"]


def test_unallowlisted_repositories_offer_the_ui_action(client, inputs):
    inputs.rows = [*_globals(delivery=("dormant", "not_allowlisted")), _repo_row(allowlisted=False)]

    check = _checks(client.get("/api/v1/setup/status").json())["repositories"]

    assert check["status"] == "action_needed"
    assert check["fix"]["ui_action"] == "allowlist_repositories"
    assert "acme/gitops" in check["reason"]


def test_unenforced_renderer_egress_is_reported(client, inputs):
    inputs.rows = [*_globals(renderer=("active", "renderer_egress_unenforced")), _repo_row()]

    check = _checks(client.get("/api/v1/setup/status").json())["renderer"]

    assert check["status"] == "action_needed"
    assert "renderer_egress_unenforced" in check["reason"]
    assert "renderer.egressFirewall.enabled=true" in check["fix"]["snippet"]


def test_warm_up_shows_an_eta_and_no_snapshot_is_pending(client, inputs):
    inputs.earliest = _now() - timedelta(minutes=5)
    inputs.rows = []

    body = client.get("/api/v1/setup/status").json()

    checks = _checks(body)
    assert checks["evidence"]["status"] == "pending"
    assert "eligible in ~" in checks["evidence"]["reason"]
    assert checks["gitops_controllers"]["status"] == "pending"
    assert body["capabilities"]["delivery"] == {"state": "pending", "reason": "controller_not_ready"}
    assert body["capabilities"]["measurement"]["state"] == "active"


def test_unreadable_inputs_degrade_to_pending(client, monkeypatch, inputs):
    async def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(setup, "_health_services", broken)
    monkeypatch.setattr(setup, "_capabilities", broken)

    response = client.get("/api/v1/setup/status")

    assert response.status_code == 200
    assert _checks(response.json())["prometheus"]["status"] == "pending"


class FakeCapabilityRepository:
    def __init__(self, rows):
        self.rows = rows
        self.added: list[dict] = []

    async def list_capabilities(self, scope=None):
        return [row for row in self.rows if scope is None or row.scope == scope]

    async def add_allowed_repositories(self, entries, *, actor, now=None):
        self.added.extend({**entry, "actor": actor} for entry in entries)
        return len(entries)


def test_suggestions_list_inferred_providers(client, monkeypatch):
    repository = FakeCapabilityRepository([*_globals(), _repo_row(allowlisted=False)])
    monkeypatch.setattr(automation_repositories, "_repository", lambda: repository)

    body = client.get("/api/v1/automation/repositories/suggestions").json()

    assert body == {
        "suggestions": [
            {
                "repo_url": REPO,
                "provider_type": "github",
                "api_base_url": None,
                "inferred_from": "host",
                "allowlisted": False,
            }
        ]
    }


def test_allowlisting_discovered_repositories(client, monkeypatch):
    repository = FakeCapabilityRepository([_repo_row(allowlisted=False)])
    monkeypatch.setattr(automation_repositories, "_repository", lambda: repository)

    response = client.post("/api/v1/automation/repositories/allowlist", json={"repo_urls": [REPO]})

    assert response.status_code == 200
    body = response.json()
    assert body["added"] == 1
    assert body["allowlisted"] == [REPO]
    assert "    - acme/gitops" in body["values_snippet"]
    assert repository.added[0]["repository"] == "acme/gitops"
    assert repository.added[0]["host"] == "github.com"


def test_allowlisting_rejects_undiscovered_repositories(client, monkeypatch):
    repository = FakeCapabilityRepository([_repo_row(allowlisted=False)])
    monkeypatch.setattr(automation_repositories, "_repository", lambda: repository)

    response = client.post(
        "/api/v1/automation/repositories/allowlist",
        json={"repo_urls": ["https://github.com/evil/other.git"]},
    )

    assert response.status_code == 422
    assert repository.added == []


def test_allowlisting_requires_the_admin_scope():
    from greenkube.auth.principal import required_scope

    assert required_scope("POST", "/api/v1/automation/repositories/allowlist") == "admin"
    assert required_scope("GET", "/api/v1/automation/repositories/suggestions") == "read"
    assert required_scope("GET", "/api/v1/setup/status") == "read"
