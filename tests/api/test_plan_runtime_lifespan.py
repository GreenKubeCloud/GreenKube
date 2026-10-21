from __future__ import annotations

import asyncio
import importlib
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from greenkube.automation.plan_runtime import PlanProvidersNotConfiguredError


def _install_lifespan_dependencies(monkeypatch, config):
    api_app = importlib.import_module("greenkube.api.app")
    database = importlib.import_module("greenkube.core.db")
    k8s_client = importlib.import_module("greenkube.core.k8s_client")

    class DatabaseManager:
        connected = False
        closed = False

        async def connect(self):
            self.connected = True

        async def close(self):
            self.closed = True

    manager = DatabaseManager()
    monkeypatch.setattr(database, "get_db_manager", lambda: manager)
    monkeypatch.setattr(api_app, "get_config", lambda: config)
    monkeypatch.setattr(k8s_client, "close_k8s_client", _async_noop)
    monkeypatch.setattr(api_app, "run_startup_recommendation_scan", _async_noop)
    return api_app, manager


async def _async_noop():
    return None


@pytest.mark.asyncio
async def test_api_lifespan_keeps_startup_available_without_provider_configuration(monkeypatch, caplog):
    config = SimpleNamespace(AUTOMATION_ENABLED=False, AUTOMATION_MODE="disabled")
    api_app, manager = _install_lifespan_dependencies(monkeypatch, config)

    async def build_plan_api_service(**kwargs):
        raise PlanProvidersNotConfiguredError("GITOPS_PROVIDERS_JSON is required.")

    monkeypatch.setattr(api_app, "build_plan_api_service", build_plan_api_service)
    app = FastAPI()

    with caplog.at_level(logging.WARNING):
        async with api_app.lifespan(app):
            assert manager.connected
            assert app.state.automation_plan_service is None
            assert app.state.ready

    assert manager.closed
    assert "GITOPS_PROVIDERS_JSON is required" in caplog.text


@pytest.mark.asyncio
async def test_api_lifespan_runs_and_cancels_inline_plan_worker_only_in_inline_mode(monkeypatch):
    config = SimpleNamespace(AUTOMATION_ENABLED=True, AUTOMATION_MODE="inline")
    api_app, _ = _install_lifespan_dependencies(monkeypatch, config)
    automation_worker = importlib.import_module("greenkube.cli.automation_worker")
    worker = object()
    service = SimpleNamespace(worker=worker)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def build_plan_api_service(**kwargs):
        assert kwargs == {"connect_db": False}
        return service

    async def run_worker(*, queue: str, plan_worker):
        assert queue == "plans"
        assert plan_worker is worker
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(api_app, "build_plan_api_service", build_plan_api_service)
    monkeypatch.setattr(automation_worker, "run_worker", run_worker)
    app = FastAPI()

    async with api_app.lifespan(app):
        await asyncio.wait_for(started.wait(), timeout=1)
        assert app.state.automation_plan_service is service
        assert len(app.state._startup_tasks) == 2

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_worker_mode_builds_api_service_without_starting_inline_task(monkeypatch):
    config = SimpleNamespace(AUTOMATION_ENABLED=True, AUTOMATION_MODE="worker")
    api_app, _ = _install_lifespan_dependencies(monkeypatch, config)
    automation_worker = importlib.import_module("greenkube.cli.automation_worker")
    service = SimpleNamespace(worker=object())
    calls = 0

    async def build_plan_api_service(**kwargs):
        return service

    async def run_worker(**kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(api_app, "build_plan_api_service", build_plan_api_service)
    monkeypatch.setattr(automation_worker, "run_worker", run_worker)
    app = FastAPI()

    async with api_app.lifespan(app):
        assert app.state.automation_plan_service is service
        assert len(app.state._startup_tasks) == 1

    assert calls == 0


@pytest.mark.asyncio
async def test_api_lifespan_wires_mounted_webhook_secret_and_delivery_repository(monkeypatch, tmp_path):
    config = SimpleNamespace(AUTOMATION_ENABLED=False, AUTOMATION_MODE="disabled")
    api_app, _ = _install_lifespan_dependencies(monkeypatch, config)
    secret_dir = tmp_path / "webhooks"
    secret_dir.mkdir()
    (secret_dir / "provider-main").write_text("provider-secret", encoding="utf-8")
    monkeypatch.setenv("GITOPS_WEBHOOK_SECRET_DIR", str(secret_dir))
    repository = object()
    repository_calls = 0

    async def get_gitops_binding_repository():
        nonlocal repository_calls
        repository_calls += 1
        return repository

    async def build_plan_api_service(**kwargs):
        raise PlanProvidersNotConfiguredError("GITOPS_PROVIDERS_JSON is required.")

    monkeypatch.setattr(api_app, "get_gitops_binding_repository", get_gitops_binding_repository)
    monkeypatch.setattr(api_app, "build_plan_api_service", build_plan_api_service)
    app = FastAPI()

    async with api_app.lifespan(app):
        assert repository_calls == 1
        assert app.state.webhook_processing_service.delivery_repository is repository
        assert await app.state.webhook_processing_service.secret_resolver("provider-main") == "provider-secret"
        assert app.state.pull_request_lifecycle_reconciler is None

    assert app.state.webhook_processing_service is None


@pytest.mark.asyncio
async def test_api_lifespan_composes_pr_lifecycle_from_plan_worker(monkeypatch, tmp_path):
    from greenkube.automation.pr_lifecycle import PullRequestLifecycleReconciler

    config = SimpleNamespace(AUTOMATION_ENABLED=False, AUTOMATION_MODE="disabled")
    api_app, _ = _install_lifespan_dependencies(monkeypatch, config)
    secret_file = tmp_path / "webhook-key"
    secret_file.write_text("provider-secret", encoding="utf-8")
    monkeypatch.setenv("GITOPS_WEBHOOK_SECRET_FILE", str(secret_file))
    monkeypatch.setattr(api_app, "get_gitops_binding_repository", _async_repository)
    worker = SimpleNamespace(
        provider_resolver=SimpleNamespace(provider_repository=object(), registry=object()),
        plans=object(),
        pull_request_repository=object(),
    )

    async def build_plan_api_service(**kwargs):
        return SimpleNamespace(worker=worker)

    monkeypatch.setattr(api_app, "build_plan_api_service", build_plan_api_service)
    app = FastAPI()

    async with api_app.lifespan(app):
        assert isinstance(app.state.pull_request_lifecycle_reconciler, PullRequestLifecycleReconciler)

    assert app.state.pull_request_lifecycle_reconciler is None


async def _async_repository():
    return object()
