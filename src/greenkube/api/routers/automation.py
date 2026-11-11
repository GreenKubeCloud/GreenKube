"""Automation status and pull-request read routes.

The former compatibility routes ``/recommendations/{id}/apply-pr``, ``/apply-pr/eligibility`` and
``/automation/operations/{id}`` were removed after their deprecation release (roadmap F5, M4):
use ``/automation/plans`` and the recommendations' ``eligibility`` field.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, List
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from greenkube.api.dependencies import (
    AutomationPlanApiService,
    get_automation_plan_service,
    get_gitops_binding_repository,
    get_pull_request_repository,
    get_recommendation_repository,
)
from greenkube.automation.delivery import configured_delivery_mode
from greenkube.automation.git.k8s_secret_resolver import MountedSecretFileResolver
from greenkube.models.metrics import PullRequestRecord
from greenkube.storage.base_gitops_repository import GitOpsProviderConfig, GitOpsRepository
from greenkube.storage.base_pull_request_repository import PullRequestRepository
from greenkube.storage.base_repository import RecommendationRepository
from greenkube.utils.time import ensure_utc

router = APIRouter()
WORKER_STALE_AFTER = timedelta(seconds=120)


@router.get("/recommendations/{rec_id}/pull-requests", response_model=List[PullRequestRecord])
async def list_recommendation_pull_requests(
    rec_id: int,
    reco_repo: RecommendationRepository = Depends(get_recommendation_repository),
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Return every pull-request attempt recorded for a recommendation."""
    record = await reco_repo.get_recommendation_by_id(rec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found.")
    return _externalize_pull_request_urls(await pr_repo.get_pull_requests_for_recommendation(rec_id))


@router.get("/automation/pull-requests", response_model=List[PullRequestRecord])
async def list_open_pull_requests(
    pr_repo: PullRequestRepository = Depends(get_pull_request_repository),
):
    """Return pull requests that are still pending or open."""
    return _externalize_pull_request_urls(await pr_repo.get_open_pull_requests())


def get_controller_heartbeats() -> Any:
    """The persisted controller loop heartbeats (dependency, overridable in tests)."""
    from greenkube.storage.controller_heartbeat_repository import get_controller_heartbeat_repository

    return get_controller_heartbeat_repository()


async def _controller_state(repository: Any, now: datetime) -> dict[str, Any]:
    """Controller loop state from the heartbeats each controller replica persists (M8)."""
    from greenkube.core.loops import HEARTBEAT_RETENTION, HEARTBEAT_STALE_AFTER_SECONDS

    state: dict[str, Any] = {
        "available": True,
        "healthy": False,
        "leader": None,
        "stale_after_seconds": HEARTBEAT_STALE_AFTER_SECONDS,
        "instances": [],
    }
    try:
        heartbeats = await repository.list_since(now - HEARTBEAT_RETENTION)
    except Exception:
        state["available"] = False
        return state
    for heartbeat in heartbeats:
        age = heartbeat.age_seconds(now)
        stale = age > HEARTBEAT_STALE_AFTER_SECONDS
        state["instances"].append(
            {
                "instance_id": heartbeat.instance_id,
                "role": heartbeat.role,
                "leader": heartbeat.leader,
                "ready": heartbeat.ready and not stale,
                "stale": stale,
                "heartbeat_at": _iso_utc(heartbeat.heartbeat_at),
                "heartbeat_age_seconds": age,
                "started_at": _iso_utc(heartbeat.started_at),
                "loops": heartbeat.loops,
            }
        )
        if heartbeat.leader and not stale and state["leader"] is None:
            state["leader"] = heartbeat.instance_id
            state["healthy"] = heartbeat.ready
    return state


@router.get("/automation/status")
async def automation_status(
    http_request: Request,
    service: AutomationPlanApiService | None = Depends(get_automation_plan_service),
    repository: GitOpsRepository = Depends(get_gitops_binding_repository),
    heartbeats: Any = Depends(get_controller_heartbeats),
):
    """Return secret-free automation readiness, worker health and queue depth."""
    from greenkube.core.config import get_config

    config = get_config()
    configured = service is not None
    # Automation is always on (F12): only automation.policy.pauseDelivery stops PR delivery.
    paused = config.automation_delivery_paused
    enabled = configured and not paused
    now = datetime.now(timezone.utc)

    backlog = None
    coverage = None
    providers: list[dict[str, Any]] = []
    try:
        backlog = await repository.get_automation_backlog_summary(stale_before=now - WORKER_STALE_AFTER)
    except Exception:
        backlog = None
    try:
        coverage = await repository.get_workload_binding_coverage(
            cluster=config.CLUSTER_NAME or "default",
            now=now,
        )
    except Exception:
        coverage = None
    try:
        configured_providers = await repository.list_provider_configs()
    except Exception:
        configured_providers = []
    token_configured = False
    for provider in configured_providers:
        if provider.id == "unresolved":
            continue
        provider_token_configured = await _provider_token_resolvable(provider)
        token_configured = token_configured or (provider.enabled and provider_token_configured)
        providers.append(
            {
                "id": provider.id,
                "type": provider.provider_type,
                "enabled": provider.enabled,
                "token_configured": provider_token_configured,
                "status": provider.status,
                "capabilities": dict(provider.capabilities),
                "last_checked_at": _iso_utc(provider.last_checked_at),
                "last_error_code": provider.last_error_code,
                "api_host": _safe_host(provider.api_base_url),
            }
        )

    latest_heartbeat = None
    worker_healthy = False
    if backlog is not None and backlog.latest_worker_heartbeat_timestamp > 0:
        latest_heartbeat = datetime.fromtimestamp(backlog.latest_worker_heartbeat_timestamp, tz=timezone.utc)
        worker_healthy = now - latest_heartbeat <= WORKER_STALE_AFTER
    supervisor = getattr(http_request.app.state, "automation_loop_supervisor", None)
    inline_loop = supervisor.health.snapshot(now) if supervisor is not None else None
    if inline_loop is not None and not inline_loop["ready"]:
        worker_healthy = False
    # Controller loops run in the controller process: read their persisted heartbeats.
    controller = await _controller_state(heartbeats, now)
    discovery_age = None
    if coverage is not None and coverage.last_refresh is not None:
        discovery_age = max(0.0, (now - ensure_utc(coverage.last_refresh)).total_seconds())

    renderer_url = os.getenv("GITOPS_RENDERER_URL", "").strip()
    queue = (
        {
            "queued": backlog.queued_operations,
            "running": backlog.running_operations,
            "retrying": backlog.retrying_operations,
            "failed": backlog.failed_operations,
            "stuck": backlog.stuck_operations,
            "pending_webhooks": backlog.pending_webhooks,
            "stuck_webhooks": backlog.stuck_webhooks,
        }
        if backlog is not None
        else None
    )
    return {
        "enabled": enabled,
        "ready": enabled and worker_healthy,
        # True only when an enabled provider's credential Secret actually resolves (D12, BUG-56).
        "token_configured": token_configured,
        "provider": "gitops" if configured else None,
        # "paused" (pauseDelivery) or "controller": the controller always executes plans,
        # optionally helped by dedicated workers (worker.replicas).
        "mode": "paused" if paused else "controller",
        "delivery_paused": paused,
        # Who opens the pull request of an approved plan (F4): direct, patch or ci.
        "delivery_mode": configured_delivery_mode(config),
        # Whether previews meeting automation.autoApprove skip the approval click (F5).
        "auto_approve": bool(config.AUTOMATION_AUTO_APPROVE_ENABLED),
        "worker": {
            "healthy": worker_healthy,
            "heartbeat_at": latest_heartbeat,
            "count": backlog.worker_count if backlog is not None else None,
            "stale": backlog.stale_workers if backlog is not None else None,
            # In-process loop supervision: state, heartbeat age, restarts, last error code (D9).
            "inline_loop": inline_loop,
        },
        # Controller loop state per replica: leader, readiness, per-loop heartbeat and errors (M8).
        "controller": controller,
        "queue": queue,
        "plans": (
            {
                "applying": backlog.applying_plans,
                "merged": backlog.merged_plans,
                "stuck": backlog.stuck_plans,
                "sync_failed": backlog.sync_failed_plans,
            }
            if backlog is not None
            else None
        ),
        "discovery": (
            {
                "last_refresh": _iso_utc(coverage.last_refresh),
                "age_seconds": discovery_age,
                "resolved": coverage.resolved,
                "total": coverage.total,
                "coverage_ratio": coverage.ratio,
            }
            if coverage is not None
            else None
        ),
        "renderer": {
            "configured": bool(renderer_url),
            "isolated": bool(renderer_url),
            "egress_firewall": _env_truthy(os.getenv("GITOPS_RENDERER_EGRESS_FIREWALL")),
        },
        "providers": providers,
    }


async def _provider_token_resolvable(provider: GitOpsProviderConfig) -> bool:
    """Whether the provider's referenced Secret key resolves to a non-empty credential.

    The value is read through the same mounted-Secret resolver the plan runtime uses and
    is never returned or logged.
    """
    if not provider.secret_name or not provider.secret_key:
        return False
    try:
        value = await MountedSecretFileResolver().resolve_secret(
            namespace=provider.secret_namespace,
            name=provider.secret_name,
            key=provider.secret_key,
        )
    except Exception:
        return False
    return bool(value and value.strip())


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def _safe_host(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return urlsplit(value.strip()).hostname
    except ValueError:
        return None


def _env_truthy(value: str | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def _configured_gitea_web_base_urls() -> dict[str, str]:
    serialized = os.getenv("GITOPS_PROVIDERS_JSON", "").strip()
    if not serialized:
        return {}
    payload = json.loads(serialized)
    providers = payload.get("providers") if isinstance(payload, dict) else payload
    if not isinstance(providers, list):
        raise ValueError("GITOPS_PROVIDERS_JSON must contain a providers list.")

    web_base_urls: dict[str, str] = {}
    for provider in providers:
        if not isinstance(provider, dict):
            raise ValueError("GITOPS_PROVIDERS_JSON provider entries must be objects.")
        if str(provider.get("type", "")).lower() != "gitea":
            continue
        web_base_url = provider.get("webBaseUrl")
        if web_base_url is None or web_base_url == "":
            continue
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("Gitea webBaseUrl requires a provider ID.")
        if not isinstance(web_base_url, str):
            raise ValueError("Gitea webBaseUrl must be a string.")
        parsed_url = urlsplit(web_base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in {"", "/"}
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("Gitea webBaseUrl must be an HTTP(S) origin.")
        web_base_urls[provider_id] = urlunsplit((parsed_url.scheme, parsed_url.netloc, "", "", ""))
    return web_base_urls


def _externalize_pull_request_urls(records: list[PullRequestRecord]) -> list[PullRequestRecord]:
    web_base_urls = _configured_gitea_web_base_urls()
    externalized: list[PullRequestRecord] = []
    for record in records:
        web_base_url = web_base_urls.get(record.provider_id or "")
        if not record.pr_url or not web_base_url:
            externalized.append(record)
            continue
        parsed_url = urlsplit(record.pr_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("Stored pull-request URL is invalid.")
        externalized.append(
            record.model_copy(
                update={
                    "pr_url": urlunsplit(
                        (
                            urlsplit(web_base_url).scheme,
                            urlsplit(web_base_url).netloc,
                            parsed_url.path,
                            "",
                            "",
                        )
                    )
                }
            )
        )
    return externalized
