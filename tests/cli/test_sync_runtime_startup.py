"""Controller composition of the GitOps sync observation loop (M8)."""

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from greenkube.automation.plan_runtime import PlanProvidersNotConfiguredError, PlanRuntimeConfigurationError


@pytest.mark.asyncio
async def test_controller_plan_runtime_is_built_once_without_reconnecting(monkeypatch):
    start_module = importlib.import_module("greenkube.cli.start")
    plan_runtime_module = importlib.import_module("greenkube.automation.plan_runtime")
    manager = object()
    runtime = object()

    async def create_plan_runtime(**kwargs):
        assert kwargs == {"db_manager": manager, "connect_db": False}
        return runtime

    monkeypatch.setattr(plan_runtime_module, "create_plan_runtime", create_plan_runtime)

    assert await start_module._create_controller_plan_runtime(manager) is runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        PlanProvidersNotConfiguredError("GITOPS_PROVIDERS_JSON is required."),
        PlanRuntimeConfigurationError("invalid policy"),
        RuntimeError("database down"),
    ],
)
async def test_sync_and_automation_loops_stay_idle_without_a_plan_runtime(monkeypatch, error):
    start_module = importlib.import_module("greenkube.cli.start")
    plan_runtime_module = importlib.import_module("greenkube.automation.plan_runtime")

    async def create_plan_runtime(**kwargs):
        raise error

    monkeypatch.setattr(plan_runtime_module, "create_plan_runtime", create_plan_runtime)

    assert await start_module._create_controller_plan_runtime(object()) is None


def test_sync_runtime_reuses_the_plan_runtime_repositories_and_feedback(monkeypatch):
    start_module = importlib.import_module("greenkube.cli.start")
    sync_runtime_module = importlib.import_module("greenkube.automation.sync_runtime")
    pull_requests = object()
    gitops_repository = object()
    provider_resolver = object()
    recommendations = object()
    plan_runtime = SimpleNamespace(
        pull_requests=pull_requests,
        gitops_repository=gitops_repository,
        provider_resolver=provider_resolver,
        recommendations=recommendations,
    )
    sentinel = object()

    def create_gitops_sync_runtime(selected_pr_repository, selected_plan_repository, *, feedback=None):
        assert selected_pr_repository is pull_requests
        assert selected_plan_repository is gitops_repository
        # The PR status-comment publisher reuses the plan runtime's repositories and providers (J5).
        assert feedback is not None
        assert feedback.pull_request_repository is pull_requests
        assert feedback.plan_repository is gitops_repository
        assert feedback.provider_resolver is provider_resolver
        assert feedback.recommendation_repository is recommendations
        return sentinel

    monkeypatch.setattr(sync_runtime_module, "create_gitops_sync_runtime", create_gitops_sync_runtime)

    assert start_module._create_gitops_sync_runtime(plan_runtime, object()) is sentinel


@pytest.mark.asyncio
async def test_sync_run_once_polls_refreshes_feedback_and_reraises_a_failed_poll():
    from greenkube.automation.sync_runtime import GitOpsSyncRuntime

    calls: list[str] = []

    class _Runtime:
        async def poll_once(self):
            calls.append("poll")
            raise ConnectionError("provider down")

        async def reconcile_feedback(self):
            calls.append("feedback")

    with pytest.raises(ConnectionError):
        await GitOpsSyncRuntime.run_once(_Runtime())  # type: ignore[arg-type]

    # The PR feedback refresh still runs after a failed poll, as in the previous loop.
    assert calls == ["poll", "feedback"]


@pytest.mark.asyncio
async def test_legacy_sync_run_loop_survives_a_failed_cycle():
    from greenkube.automation.sync_runtime import GitOpsSyncRuntime

    stop = asyncio.Event()
    polls = 0

    class _Runtime:
        interval_seconds = 0.01

        async def run_once(self):
            nonlocal polls
            polls += 1
            if polls == 2:
                stop.set()
            raise ConnectionError("provider down")

    await GitOpsSyncRuntime.run(_Runtime(), stop)  # type: ignore[arg-type]

    assert polls == 2


@pytest.mark.asyncio
async def test_discovery_run_once_reraises_after_logging_and_the_legacy_loop_survives(caplog):
    from greenkube.gitops.discovery.runtime import GitOpsDiscoveryRuntime

    stop = asyncio.Event()
    refreshes = 0

    class _Runtime:
        interval_seconds = 0.01

        async def refresh(self):
            nonlocal refreshes
            refreshes += 1
            if refreshes == 2:
                stop.set()
            raise ConnectionError("api server unreachable")

        async def run_once(self):
            return await GitOpsDiscoveryRuntime.run_once(self)  # type: ignore[arg-type]

    with pytest.raises(ConnectionError):
        await GitOpsDiscoveryRuntime.run_once(_Runtime())  # type: ignore[arg-type]
    refreshes = 0
    await GitOpsDiscoveryRuntime.run_periodic(_Runtime(), stop_event=stop)  # type: ignore[arg-type]

    assert refreshes == 2
