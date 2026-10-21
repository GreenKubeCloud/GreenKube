import asyncio
import importlib
from types import SimpleNamespace

import pytest

from greenkube.automation.plan_runtime import PlanProvidersNotConfiguredError


@pytest.mark.asyncio
async def test_start_composes_and_cancels_gitops_sync_observer(monkeypatch):
    start_module = importlib.import_module("greenkube.cli.start")
    plan_runtime_module = importlib.import_module("greenkube.automation.plan_runtime")
    sync_runtime_module = importlib.import_module("greenkube.automation.sync_runtime")
    manager = object()
    pull_requests = object()
    gitops_repository = object()
    plan_runtime = SimpleNamespace(pull_requests=pull_requests, gitops_repository=gitops_repository)
    runtime_started = asyncio.Event()
    runtime_stopped = asyncio.Event()

    async def create_plan_runtime(**kwargs):
        assert kwargs == {"db_manager": manager, "connect_db": False}
        return plan_runtime

    class SyncRuntime:
        async def run(self, stop_event):
            runtime_started.set()
            try:
                await stop_event.wait()
            finally:
                runtime_stopped.set()

    def create_gitops_sync_runtime(selected_pr_repository, selected_plan_repository):
        assert selected_pr_repository is pull_requests
        assert selected_plan_repository is gitops_repository
        return SyncRuntime()

    monkeypatch.setattr(plan_runtime_module, "create_plan_runtime", create_plan_runtime)
    monkeypatch.setattr(sync_runtime_module, "create_gitops_sync_runtime", create_gitops_sync_runtime)

    task = await start_module._start_gitops_sync_observer(manager)
    assert task is not None
    await runtime_started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert runtime_stopped.is_set()


@pytest.mark.asyncio
async def test_sync_observer_uses_controller_leader_gate(monkeypatch):
    start_module = importlib.import_module("greenkube.cli.start")
    plan_runtime_module = importlib.import_module("greenkube.automation.plan_runtime")
    sync_runtime_module = importlib.import_module("greenkube.automation.sync_runtime")
    plan_runtime = SimpleNamespace(pull_requests=object(), gitops_repository=object())

    def gate():
        return False

    runtime_started = asyncio.Event()
    runtime_stopped = asyncio.Event()

    async def create_plan_runtime(**kwargs):
        return plan_runtime

    class SyncRuntime:
        async def run(self, stop_event, *, should_run=None):
            assert should_run is gate
            runtime_started.set()
            try:
                await stop_event.wait()
            finally:
                runtime_stopped.set()

    monkeypatch.setattr(plan_runtime_module, "create_plan_runtime", create_plan_runtime)
    monkeypatch.setattr(sync_runtime_module, "create_gitops_sync_runtime", lambda *_: SyncRuntime())

    task = await start_module._start_gitops_sync_observer(object(), should_run=gate)
    assert task is not None
    await runtime_started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert runtime_stopped.is_set()


@pytest.mark.asyncio
async def test_sync_observer_stays_disabled_without_provider_configuration(monkeypatch):
    start_module = importlib.import_module("greenkube.cli.start")
    plan_runtime_module = importlib.import_module("greenkube.automation.plan_runtime")

    async def create_plan_runtime(**kwargs):
        raise PlanProvidersNotConfiguredError("GITOPS_PROVIDERS_JSON is required.")

    monkeypatch.setattr(plan_runtime_module, "create_plan_runtime", create_plan_runtime)

    assert await start_module._start_gitops_sync_observer(object()) is None
