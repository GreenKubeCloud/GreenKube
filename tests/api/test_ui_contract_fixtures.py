"""The JSON the UI tests use is the JSON the API returns (F1, F2, K3, K6 contract).

Each fixture in ``frontend/tests/fixtures/api`` is generated here from the
real FastAPI app (only its storage and health inputs are faked) and the
vitest suites import exactly these files. A response change fails this test
until the fixtures are regenerated with ``GREENKUBE_UPDATE_SNAPSHOTS=1``,
which then shows the UI tests the new contract.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from greenkube.api.routers import automation_repositories, setup
from greenkube.models.health import ServiceHealth, ServiceStatus
from greenkube.models.metrics import RecommendationRecord, RecommendationType
from greenkube.models.recommendation_v2 import RecommendationRevision
from greenkube.storage.automation_capability_repository import CapabilityRow

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "frontend" / "tests" / "fixtures" / "api"
SETUP_JS = ROOT / "frontend" / "src" / "lib" / "setup.js"
REPO = "https://github.com/acme/gitops.git"
OTHER_REPO = "https://gitea.example.com/platform/apps.git"
#: Fixed record timestamps: they only travel through the API, never meet the wall clock.
CREATED = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _globals(**states: tuple[str, str | None]) -> list[CapabilityRow]:
    values: dict[str, tuple[str, str | None]] = {
        "discovery": ("active", None),
        "preview": ("active", None),
        "delivery": ("active", None),
        "sync_tracking": ("active", None),
        "webhooks": ("dormant", "webhook_secret_missing"),
        "measurement": ("active", None),
        "renderer": ("active", None),
    }
    values.update(states)
    return [
        CapabilityRow("global", name, state, {"reason": reason, "controllers": ["argocd"]}, _now())
        for name, (state, reason) in values.items()
    ]


def _repo_row(url: str, repository: str, *, allowlisted: bool, provider_type: str | None) -> CapabilityRow:
    return CapabilityRow(
        "repository",
        url,
        "active" if allowlisted else "dormant",
        {
            "repository": repository,
            "provider_type": provider_type,
            "api_base_url": "https://gitea.example.com/api/v1" if provider_type == "gitea" else None,
            "inferred_from": "probe" if provider_type == "gitea" else "host",
            "allowlisted": allowlisted,
        },
        _now(),
    )


def _repositories() -> list[CapabilityRow]:
    return [
        _repo_row(REPO, "acme/gitops", allowlisted=True, provider_type="github"),
        _repo_row(OTHER_REPO, "platform/apps", allowlisted=False, provider_type="gitea"),
    ]


def _healthy_services() -> dict[str, ServiceHealth]:
    return {
        "prometheus": ServiceHealth(name="prometheus", status=ServiceStatus.HEALTHY),
        "opencost": ServiceHealth(name="opencost", status=ServiceStatus.HEALTHY),
        "electricity_maps": ServiceHealth(name="electricity_maps", status=ServiceStatus.HEALTHY),
    }


SCENARIOS: dict[str, dict[str, Any]] = {
    # A first install: sources failing, a repository to allow, a token not bound to a host,
    # an unenforced renderer egress.
    "setup_status_incomplete": {
        "services": {
            "prometheus": ServiceHealth(name="prometheus", status=ServiceStatus.HEALTHY),
            "opencost": ServiceHealth(name="opencost", status=ServiceStatus.UNREACHABLE),
            "electricity_maps": ServiceHealth(name="electricity_maps", status=ServiceStatus.UNCONFIGURED),
        },
        "sources": [
            {"id": "greenkube.rightsizing", "status": "healthy", "enabled": "auto"},
            {"id": "krr", "status": "failing", "enabled": "true"},
        ],
        "rows": lambda: [
            *_globals(
                delivery=("dormant", "credentials_host_ambiguous"),
                sync_tracking=("dormant", "credentials_host_ambiguous"),
                renderer=("active", "renderer_egress_unenforced"),
            ),
            *_repositories(),
        ],
        "heartbeat": None,
    },
    # The token names a host none of the discovered repositories use.
    "setup_status_host_mismatch": {
        "services": _healthy_services(),
        "sources": [{"id": "greenkube.rightsizing", "status": "healthy", "enabled": "auto"}],
        "rows": lambda: [*_globals(delivery=("dormant", "credentials_host_mismatch")), _repositories()[0]],
        "heartbeat": None,
    },
    # automation.policy.pauseDelivery=true: the only off switch.
    "setup_status_paused": {
        "services": _healthy_services(),
        "sources": [{"id": "greenkube.rightsizing", "status": "healthy", "enabled": "auto"}],
        "rows": lambda: [*_globals(delivery=("dormant", "delivery_paused")), _repositories()[0]],
        "heartbeat": None,
    },
    "setup_status_complete": {
        "services": _healthy_services(),
        "sources": [{"id": "greenkube.rightsizing", "status": "healthy", "enabled": "auto"}],
        "rows": lambda: [*_globals(), _repositories()[0]],
        "heartbeat": "now",
    },
}


#: Keys stamped with the request time; the fixture keeps a fixed value instead.
VOLATILE_KEYS = frozenset({"evaluated_at"})


def _scrub(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: (CREATED.isoformat() if key in VOLATILE_KEYS and value is not None else _scrub(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_scrub(item) for item in payload]
    return payload


def _render(payload: Any) -> str:
    return json.dumps(_scrub(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _check_fixture(name: str, payload: Any) -> None:
    path = FIXTURES / f"{name}.json"
    rendered = _render(payload)
    if os.environ.get("GREENKUBE_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    assert path.exists(), f"missing UI fixture {path}; run with GREENKUBE_UPDATE_SNAPSHOTS=1"
    assert rendered == path.read_text(encoding="utf-8"), (
        f"{path.name} no longer matches the API response; regenerate it with GREENKUBE_UPDATE_SNAPSHOTS=1 "
        "and update the UI if the contract changed"
    )


def _fake_setup_inputs(monkeypatch, scenario: dict[str, Any]) -> None:
    async def services():
        return scenario["services"]

    async def sources():
        return scenario["sources"]

    async def rows():
        # Built per request: the readiness snapshot must be fresh (stamped now).
        return scenario["rows"]()

    async def heartbeat():
        return _now() if scenario["heartbeat"] == "now" else None

    async def earliest():
        return _now() - timedelta(days=30)

    monkeypatch.setattr(setup, "_health_services", services)
    monkeypatch.setattr(setup, "_source_statuses", sources)
    monkeypatch.setattr(setup, "_capabilities", rows)
    monkeypatch.setattr(setup, "_executor_heartbeat", heartbeat)
    monkeypatch.setattr(setup, "_earliest_sample", earliest)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_setup_status_fixtures(client, monkeypatch, name):
    _fake_setup_inputs(monkeypatch, SCENARIOS[name])
    response = client.get("/api/v1/setup/status")
    assert response.status_code == 200
    _check_fixture(name, response.json())


class _Capabilities:
    def __init__(self, rows):
        self.rows = rows

    async def list_capabilities(self, scope=None):
        return [row for row in self.rows if scope is None or row.scope == scope]

    async def add_allowed_repositories(self, entries, *, actor, now=None):
        return len(entries)


def test_repository_suggestion_and_allowlist_fixtures(client, monkeypatch):
    repository = _Capabilities([*_globals(), *_repositories()])
    monkeypatch.setattr(automation_repositories, "_repository", lambda: repository)

    suggestions = client.get("/api/v1/automation/repositories/suggestions")
    assert suggestions.status_code == 200
    _check_fixture("repository_suggestions", suggestions.json())

    allowed = client.post("/api/v1/automation/repositories/allowlist", json={"repo_urls": [OTHER_REPO]})
    assert allowed.status_code == 200
    _check_fixture("repository_allowlist", allowed.json())


CONSENSUS = {
    "policy": "safest",
    "resource": "cpu_request_m",
    "value": 300,
    "chosen_from": ["goldilocks"],
    "agreement": 0.4,
    "contested": True,
    "reasons": [
        "agreement 0.40 is below 0.5",
        "sources disagree on the direction: decrease (goldilocks, krr); increase (push:ci)",
    ],
    "spread": {"cpu_request_m": {"goldilocks": 300, "vpa": 250, "krr": 250, "push:ci": 750}},
    "independent_votes": 3,
    "source_ids": ["goldilocks", "vpa", "krr", "push:ci"],
    "votes": [
        {
            "source_id": "goldilocks",
            "source": "vpa",
            "family": "vpa",
            "signal_id": "sig-goldilocks",
            "value": 300,
            "direction": "decrease",
            "counted": True,
            "supports_chosen": True,
        },
        {
            "source_id": "vpa",
            "source": "vpa",
            "family": "vpa",
            "signal_id": "sig-vpa",
            "value": 250,
            "direction": "decrease",
            "counted": False,
            "supports_chosen": False,
        },
        {
            "source_id": "krr",
            "source": "krr",
            "family": "krr",
            "signal_id": "sig-krr",
            "value": 250,
            "direction": "decrease",
            "counted": True,
            "supports_chosen": False,
        },
        {
            "source_id": "push:ci",
            "source": "push:ci",
            "family": "push:ci",
            "signal_id": "sig-push",
            "value": 750,
            "direction": "increase",
            "counted": True,
            "supports_chosen": False,
        },
    ],
}


def test_recommendation_v2_detail_fixture(client, mock_reco_repo):
    record = RecommendationRecord(
        id=12,
        pod_name="api",
        namespace="shop",
        container_name="web",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Deployment 'api' container 'web' CPU request 500m → 300m: safest consensus of 3 sources.",
        reason="Consensus across sources.",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=300,
        source="vpa",
        sources=["vpa", "krr", "push:ci"],
        fingerprint="legacy-fp",
        revision=3,
        consensus=CONSENSUS,
        created_at=CREATED,
        updated_at=CREATED,
    )
    mock_reco_repo.get_recommendation_by_id = AsyncMock(return_value=record)
    mock_reco_repo.get_revisions = AsyncMock(
        return_value=[
            RecommendationRevision(
                recommendation_id=12,
                revision=4,
                state="queued",
                snapshot={"recommended_cpu_request_millicores": 260},
                created_at=CREATED + timedelta(days=2),
            ),
            RecommendationRevision(
                recommendation_id=12,
                revision=2,
                state="superseded",
                snapshot={"recommended_cpu_request_millicores": 320},
                created_at=CREATED + timedelta(days=1),
            ),
        ]
    )

    response = client.get("/api/v2/recommendations/12")

    assert response.status_code == 200
    body = response.json()
    assert body["queued_revision"] == 4 and body["source_ids"] == ["goldilocks", "vpa", "krr", "push:ci"]
    _check_fixture("recommendation_v2_detail", body)


def test_every_ui_action_the_api_emits_is_known_to_the_ui():
    """``fix.ui_action`` ids are routed by ``UI_ACTIONS`` in ``frontend/src/lib/setup.js``."""
    emitted = set(re.findall(r'ui_action="([a-z_]+)"', (ROOT / "src/greenkube/api/routers/setup.py").read_text()))
    assert emitted, "the setup router emits no ui_action"
    assert emitted <= set(setup.UI_ACTIONS)
    source = SETUP_JS.read_text(encoding="utf-8")
    block = source[source.index("const UI_ACTIONS") : source.index("};", source.index("const UI_ACTIONS"))]
    known = set(re.findall(r"^\s*([a-z_]+):", block, re.M))
    assert known == set(setup.UI_ACTIONS)
