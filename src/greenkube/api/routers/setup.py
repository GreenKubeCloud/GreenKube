"""Guided setup checklist (roadmap F1): ``GET /api/v1/setup/status``.

Aggregates, read-only, what the install still needs: data sources, recommendation
sources, GitOps controllers, discovered repositories and their allowlist state, Git
credentials, the isolated renderer, the plan executor and the evidence warm-up. The
automation capabilities come from the controller's ``AutomationReadiness``
reconciler (``automation_capabilities``, F12); the API never computes them itself.

Every check has a ``status`` (``ok``, ``action_needed``, ``blocked``,
``not_applicable``, ``pending``), a ``reason``, a ``fix`` (text, an optional
copy-paste snippet and an optional UI action) and a ``docs_url``. ``complete`` is
true when every check is ``ok`` or ``not_applicable``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from greenkube.storage.automation_capability_repository import (
    SCOPE_GLOBAL,
    SCOPE_REPOSITORY,
    AutomationCapabilityRepository,
    CapabilityRow,
)

logger = logging.getLogger(__name__)

router = APIRouter()

Category = Literal[
    "data_sources",
    "recommendation_sources",
    "gitops",
    "repositories",
    "credentials",
    "renderer",
    "executor",
    "evidence",
]
CheckStatus = Literal["ok", "action_needed", "blocked", "not_applicable", "pending"]

DOCS = "https://github.com/GreenKubeCloud/GreenKube/blob/main/docs"
#: A plan executor heartbeat older than this is stale (matches the automation status route).
EXECUTOR_STALE_AFTER = timedelta(seconds=120)
#: A readiness snapshot older than this means the controller is not reconciling.
SNAPSHOT_STALE_AFTER = timedelta(minutes=5)
SUMMARY_CAPABILITIES = ("discovery", "delivery", "sync_tracking", "measurement")


class SetupFix(BaseModel):
    text: str
    snippet: Optional[str] = None
    ui_action: Optional[str] = None


class SetupCheck(BaseModel):
    id: str
    category: Category
    title: str
    status: CheckStatus
    reason: str
    fix: SetupFix
    docs_url: str


class CapabilitySummary(BaseModel):
    state: str
    reason: Optional[str] = None


class SetupStatus(BaseModel):
    complete: bool
    checks: List[SetupCheck] = Field(default_factory=list)
    capabilities: Dict[str, CapabilitySummary] = Field(default_factory=dict)


def _check(
    check_id: str,
    category: Category,
    title: str,
    status: CheckStatus,
    reason: str,
    fix: str,
    docs: str,
    *,
    snippet: Optional[str] = None,
    ui_action: Optional[str] = None,
) -> SetupCheck:
    return SetupCheck(
        id=check_id,
        category=category,
        title=title,
        status=status,
        reason=reason,
        fix=SetupFix(text=fix, snippet=snippet, ui_action=ui_action),
        docs_url=f"{DOCS}/{docs}",
    )


# --------------------------------------------------------------------------- inputs


async def _health_services() -> Dict[str, Any]:
    from greenkube.core.health import run_health_checks

    return dict((await run_health_checks()).services)


async def _source_statuses() -> List[Dict[str, Any]]:
    from greenkube.api.routers.sources import get_signal_repository, get_source_catalog, list_sources

    response = await list_sources(repo=await get_signal_repository(), catalog=get_source_catalog())
    return [{"id": item.id, "status": item.status, "enabled": item.enabled} for item in response.sources]


async def _capabilities() -> List[CapabilityRow]:
    from greenkube.core.db import get_db_manager

    return await AutomationCapabilityRepository(get_db_manager()).list_capabilities()


async def _executor_heartbeat() -> Optional[datetime]:
    from greenkube.api.dependencies import get_gitops_binding_repository

    repository = await get_gitops_binding_repository()
    backlog = await repository.get_automation_backlog_summary(
        stale_before=datetime.now(timezone.utc) - EXECUTOR_STALE_AFTER
    )
    if backlog is None or backlog.latest_worker_heartbeat_timestamp <= 0:
        return None
    return datetime.fromtimestamp(backlog.latest_worker_heartbeat_timestamp, tz=timezone.utc)


async def _earliest_sample() -> Optional[datetime]:
    from greenkube.core.config import get_config
    from greenkube.core.db import get_db_manager
    from greenkube.storage.base_workload_usage_repository import create_workload_usage_repository

    return await create_workload_usage_repository(get_db_manager(), get_config().DB_TYPE).earliest_input_time()


async def _safe(loader: Callable[[], Awaitable[Any]], what: str) -> tuple[Any, bool]:
    try:
        return await loader(), True
    except Exception as error:
        logger.warning("Setup status could not read %s (%s).", what, type(error).__name__)
        return None, False


# --------------------------------------------------------------------------- checks


def _data_source_checks(services: Optional[Dict[str, Any]], available: bool) -> List[SetupCheck]:
    if not available or services is None:
        return [
            _check(
                "prometheus",
                "data_sources",
                "Prometheus",
                "pending",
                "Data-source health could not be read yet.",
                "Retry in a minute; check the API logs if it persists.",
                "configuration.md#prometheus--opencost-integration-configprometheus-configopencost",
            )
        ]
    checks: List[SetupCheck] = []
    prometheus = services.get("prometheus")
    status = _service_status(prometheus)
    checks.append(
        _check(
            "prometheus",
            "data_sources",
            "Prometheus",
            "ok" if status == "healthy" else "action_needed",
            "Prometheus answers." if status == "healthy" else f"Prometheus is {status or 'not found'}.",
            "GreenKube discovers Prometheus in the cluster. If discovery fails, set its URL.",
            "configuration.md#prometheus--opencost-integration-configprometheus-configopencost",
            snippet="helm upgrade greenkube greenkube/greenkube -n greenkube --reuse-values \\\n"
            "  --set config.prometheus.url=http://prometheus-server.monitoring.svc:80",
        )
    )
    opencost = _service_status(services.get("opencost"))
    checks.append(
        _check(
            "opencost",
            "data_sources",
            "OpenCost",
            "ok" if opencost == "healthy" else "not_applicable",
            "OpenCost answers: observed costs are used."
            if opencost == "healthy"
            else "OpenCost is not detected: costs use the bundled list prices (optional).",
            "Install OpenCost, or set config.opencost.url, for observed costs.",
            "configuration.md#prometheus--opencost-integration-configprometheus-configopencost",
        )
    )
    provider = "wattnet" if "wattnet" in services and not _inactive(services.get("wattnet")) else "electricity_maps"
    intensity = _service_status(services.get(provider))
    checks.append(
        _check(
            "carbon_intensity",
            "data_sources",
            "Grid carbon intensity",
            "ok" if intensity == "healthy" else "not_applicable",
            "Live grid intensity is collected."
            if intensity == "healthy"
            else "No intensity provider is configured: the default grid intensity is used (optional).",
            "Set an Electricity Maps token (or Wattnet credentials) for live grid intensity.",
            "configuration.md#general-configuration-config",
            snippet="helm upgrade greenkube greenkube/greenkube -n greenkube --reuse-values \\\n"
            "  --set secrets.electricityMapsToken=<token>",
        )
    )
    return checks


def _service_status(service: Any) -> Optional[str]:
    if service is None:
        return None
    status = getattr(service, "status", None)
    return getattr(status, "value", status)


def _inactive(service: Any) -> bool:
    return bool(getattr(service, "inactive", False)) or _service_status(service) == "unconfigured"


def _source_check(sources: Optional[List[Dict[str, Any]]], available: bool) -> SetupCheck:
    title = "Recommendation sources"
    docs = "sources.md"
    if not available or sources is None:
        return _check(
            "recommendation_sources",
            "recommendation_sources",
            title,
            "pending",
            "Sources could not be read.",
            "Retry.",
            docs,
        )
    failing = [item["id"] for item in sources if item["status"] in {"failing", "misconfigured"}]
    healthy = [item["id"] for item in sources if item["status"] in {"healthy", "ok", "degraded"}]
    if failing:
        return _check(
            "recommendation_sources",
            "recommendation_sources",
            title,
            "action_needed",
            f"Failing sources: {', '.join(sorted(failing))}.",
            "Open the Sources page for the error and the fix of each source.",
            docs,
            ui_action="open_sources",
        )
    if healthy:
        return _check(
            "recommendation_sources",
            "recommendation_sources",
            title,
            "ok",
            f"{len(healthy)} source(s) produced signals.",
            "Optional sources (VPA, Karpenter, plugins) activate when they are detected.",
            docs,
        )
    return _check(
        "recommendation_sources",
        "recommendation_sources",
        title,
        "pending",
        "No source has run yet: the first scan follows the first collection.",
        "Wait for the first scan, or queue one with POST /api/v1/optimization/runs.",
        docs,
    )


def _capability(rows: Dict[str, CapabilityRow], name: str) -> Optional[CapabilityRow]:
    return rows.get(name)


def _reason(row: Optional[CapabilityRow]) -> Optional[str]:
    return row.payload.get("reason") if row is not None else None


def _gitops_check(row: Optional[CapabilityRow], snapshot_fresh: bool) -> SetupCheck:
    title = "GitOps controller (Argo CD or Flux)"
    docs = "automation.md#capability-activation"
    if row is None or not snapshot_fresh:
        return _check(
            "gitops_controllers",
            "gitops",
            title,
            "pending",
            "The controller has not reported automation readiness yet.",
            "Wait a minute after install; check the controller logs if it persists.",
            docs,
        )
    if row.state == "active":
        controllers = ", ".join(row.payload.get("controllers") or []) or "workload sources"
        return _check(
            "gitops_controllers", "gitops", title, "ok", f"Discovered: {controllers}.", "Nothing to do.", docs
        )
    if row.state == "pending":
        return _check(
            "gitops_controllers", "gitops", title, "pending", "Discovery has not run yet.", "Wait for discovery.", docs
        )
    return _check(
        "gitops_controllers",
        "gitops",
        title,
        "action_needed",
        "No Argo CD or Flux installation was found. GreenKube still recommends and measures; "
        "pull requests need a GitOps controller.",
        "Install Argo CD or Flux and point it at your configuration repository; discovery re-checks every interval.",
        docs,
    )


def _repository_check(repos: List[CapabilityRow], snapshot_fresh: bool) -> SetupCheck:
    title = "Repositories"
    docs = "automation.md#repository-allowlist"
    if not snapshot_fresh:
        return _check("repositories", "repositories", title, "pending", "Readiness not reported yet.", "Wait.", docs)
    if not repos:
        return _check(
            "repositories",
            "repositories",
            title,
            "pending",
            "No repository is referenced by a discovered Argo CD or Flux source yet.",
            "Once Argo CD or Flux manages workloads, their repositories appear here.",
            docs,
        )
    unknown = [
        row.payload.get("repository") or row.subject for row in repos if row.payload.get("provider_type") is None
    ]
    blocked = [row.payload.get("repository") or row.subject for row in repos if not row.payload.get("allowlisted")]
    if blocked:
        return _check(
            "repositories",
            "repositories",
            title,
            "action_needed",
            f"Not allowlisted: {', '.join(sorted(blocked))}.",
            "Allow the repositories GreenKube may open pull requests against.",
            docs,
            ui_action="allowlist_repositories",
        )
    if unknown:
        return _check(
            "repositories",
            "repositories",
            title,
            "action_needed",
            f"Provider not identified for: {', '.join(sorted(unknown))}.",
            "Declare the provider of a self-hosted Git server (type and apiBaseUrl).",
            "automation.md#provider-inference",
            snippet="automation:\n  providers:\n    - id: my-gitlab\n      type: gitlab\n"
            "      apiBaseUrl: https://git.example.com/api/v4\n      secretName: greenkube-git-credentials\n"
            "      secretKey: token",
        )
    return _check(
        "repositories",
        "repositories",
        title,
        "ok",
        f"{len(repos)} repository(ies) discovered and allowlisted.",
        "Nothing to do.",
        docs,
    )


def _credentials_check(
    delivery: Optional[CapabilityRow], snapshot_fresh: bool, namespace: str, secret_name: str
) -> SetupCheck:
    title = "Git credentials"
    docs = "installation.md#optional-connect-git"
    secret_snippet = (
        f"kubectl -n {namespace} create secret generic {secret_name} \\\n"
        "  --from-literal=token=<token with write access to the repositories>"
    )
    if delivery is None or not snapshot_fresh:
        return _check("git_credentials", "credentials", title, "pending", "Readiness not reported yet.", "Wait.", docs)
    reason = _reason(delivery)
    if delivery.state == "active":
        return _check(
            "git_credentials", "credentials", title, "ok", "Pull-request delivery is active.", "Nothing to do.", docs
        )
    if reason == "delivery_paused":
        return _check(
            "git_credentials",
            "credentials",
            title,
            "blocked",
            "Delivery is paused by policy (automation.policy.pauseDelivery).",
            "Set pauseDelivery back to false once the change freeze is over.",
            "automation.md#pause-delivery",
            snippet="helm upgrade greenkube greenkube/greenkube -n greenkube --reuse-values \\\n"
            "  --set automation.policy.pauseDelivery=false",
        )
    if reason in {"credentials_missing", "github_app_not_supported"}:
        return _check(
            "git_credentials",
            "credentials",
            title,
            "action_needed",
            "No Git credentials are mounted: previews and pull requests wait for them."
            if reason == "credentials_missing"
            else "GitHub App credentials were found; use a token until GitHub App delivery is available.",
            "Create one Secret; delivery activates within one to two minutes, without upgrade or restart.",
            docs,
            snippet=secret_snippet,
        )
    if reason in {"no_repository_discovered", "provider_unknown", "not_allowlisted"}:
        return _check(
            "git_credentials",
            "credentials",
            title,
            "pending",
            "Delivery waits for a discovered, allowlisted repository.",
            "See the Repositories check.",
            docs,
        )
    return _check(
        "git_credentials",
        "credentials",
        title,
        "pending",
        "Credentials are mounted; delivery activates once a workload binding resolves.",
        "Wait for the next discovery run.",
        docs,
    )


def _renderer_check(row: Optional[CapabilityRow], snapshot_fresh: bool) -> SetupCheck:
    title = "Isolated renderer"
    docs = "configuration.md#isolated-renderer-renderer"
    if row is None or not snapshot_fresh:
        return _check("renderer", "renderer", title, "pending", "Readiness not reported yet.", "Wait.", docs)
    reason = _reason(row)
    if row.state != "active":
        return _check(
            "renderer",
            "renderer",
            title,
            "blocked",
            "The renderer does not answer: Helm and Kustomize previews are unavailable "
            "(raw-manifest repositories are not affected).",
            "Check the greenkube-renderer Deployment and its logs.",
            docs,
        )
    if reason == "renderer_egress_unenforced":
        return _check(
            "renderer",
            "renderer",
            title,
            "action_needed",
            "renderer_egress_unenforced: the cluster network plugin does not enforce the renderer's egress-deny "
            "NetworkPolicy. Previews still work, with a warning.",
            "Use a CNI that enforces NetworkPolicy, or enable the init-container firewall (needs NET_ADMIN).",
            docs,
            snippet="helm upgrade greenkube greenkube/greenkube -n greenkube --reuse-values \\\n"
            "  --set renderer.egressFirewall.enabled=true",
        )
    return _check(
        "renderer", "renderer", title, "ok", "The renderer answers and its egress is denied.", "Nothing.", docs
    )


def _executor_check(heartbeat: Optional[datetime], delivery: Optional[CapabilityRow], now: datetime) -> SetupCheck:
    title = "Plan executor"
    docs = "architecture.md#process-topology"
    if heartbeat is not None and now - heartbeat <= EXECUTOR_STALE_AFTER:
        return _check("executor", "executor", title, "ok", "The plan executor is running.", "Nothing to do.", docs)
    if delivery is None or delivery.state != "active":
        return _check(
            "executor",
            "executor",
            title,
            "not_applicable",
            "Plans execute in the controller once delivery is active.",
            "Nothing to do until delivery activates.",
            docs,
        )
    return _check(
        "executor",
        "executor",
        title,
        "action_needed",
        "Delivery is active but no plan executor heartbeat was recorded recently.",
        "Check the greenkube-controller Deployment (its /readyz lists the plan-executor loop).",
        docs,
    )


def _evidence_check(earliest: Optional[datetime], available: bool, now: datetime) -> SetupCheck:
    from greenkube.core.config import get_config
    from greenkube.utils.time import parse_query_step_seconds

    title = "Evidence warm-up"
    docs = "recommendation.md#sources-evidence-and-ranking"
    cfg = get_config()
    try:
        step = parse_query_step_seconds(cfg.PROMETHEUS_QUERY_RANGE_STEP)
    except Exception:
        step = 300
    required = timedelta(seconds=int(cfg.RECOMMENDATION_MIN_SAMPLES) * step)
    if not available:
        return _check("evidence", "evidence", title, "pending", "Collection state could not be read.", "Retry.", docs)
    if earliest is None:
        return _check(
            "evidence",
            "evidence",
            title,
            "pending",
            "No metrics collected yet: the controller collects every query step.",
            "Wait for the first collection (check Prometheus if it does not come).",
            docs,
        )
    eligible_at = earliest + required
    if now >= eligible_at:
        return _check(
            "evidence", "evidence", title, "ok", "Enough history to recommend and open PRs.", "Nothing to do.", docs
        )
    remaining = eligible_at - now
    hours = max(1, round(remaining.total_seconds() / 3600))
    return _check(
        "evidence",
        "evidence",
        title,
        "pending",
        f"Collecting evidence: recommendations become eligible in ~{hours}h "
        f"({cfg.RECOMMENDATION_MIN_SAMPLES} samples).",
        "Nothing to do: evidence accumulates on its own.",
        docs,
    )


def _summary(rows: Dict[str, CapabilityRow], snapshot_fresh: bool) -> Dict[str, CapabilitySummary]:
    summary: Dict[str, CapabilitySummary] = {}
    for name in SUMMARY_CAPABILITIES:
        row = rows.get(name)
        if name == "measurement":
            summary[name] = CapabilitySummary(state="active")
        elif row is None or not snapshot_fresh:
            summary[name] = CapabilitySummary(state="pending", reason="controller_not_ready")
        else:
            summary[name] = CapabilitySummary(state=row.state, reason=_reason(row))
    return summary


@router.get("/setup/status", response_model=SetupStatus)
async def setup_status() -> SetupStatus:
    """The guided setup checklist (F1) and the automation capabilities (F12)."""
    import os

    now = datetime.now(timezone.utc)
    services, services_ok = await _safe(_health_services, "data sources")
    sources, sources_ok = await _safe(_source_statuses, "recommendation sources")
    capability_rows, _ = await _safe(_capabilities, "automation capabilities")
    heartbeat, _ = await _safe(_executor_heartbeat, "the plan executor heartbeat")
    earliest, earliest_ok = await _safe(_earliest_sample, "collected metrics")

    rows = capability_rows or []
    globals_ = {row.subject: row for row in rows if row.scope == SCOPE_GLOBAL}
    repos = [row for row in rows if row.scope == SCOPE_REPOSITORY]
    stamps = [row.updated_at for row in globals_.values() if row.updated_at is not None]
    snapshot_fresh = bool(stamps) and now - max(stamps) <= SNAPSHOT_STALE_AFTER
    from greenkube.core.config import get_config

    namespace = os.getenv("POD_NAMESPACE") or "greenkube"
    secret_name = getattr(get_config(), "GITOPS_DEFAULT_CREDENTIALS_SECRET", "") or "greenkube-git-credentials"

    checks = [
        *_data_source_checks(services, services_ok),
        _source_check(sources, sources_ok),
        _gitops_check(globals_.get("discovery"), snapshot_fresh),
        _repository_check(repos, snapshot_fresh),
        _credentials_check(globals_.get("delivery"), snapshot_fresh, namespace, secret_name),
        _renderer_check(globals_.get("renderer"), snapshot_fresh),
        _executor_check(heartbeat, globals_.get("delivery"), now),
        _evidence_check(earliest, earliest_ok, now),
    ]
    complete = all(check.status in ("ok", "not_applicable") for check in checks)
    return SetupStatus(complete=complete, checks=checks, capabilities=_summary(globals_, snapshot_fresh))
