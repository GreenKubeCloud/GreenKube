import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from greenkube.automation.plan_runtime import PlanProvidersNotConfiguredError
from greenkube.automation.plan_worker import PlanWorker
from greenkube.cli import automation_worker
from greenkube.storage.automation_capability_repository import SCOPE_GLOBAL, CapabilityRow


@pytest.fixture(autouse=True)
def _delivery_open(monkeypatch, request):
    """Most tests exercise the queue loop with delivery active; gate tests opt out."""
    if request.node.name.startswith("test_gate_"):
        return

    async def allowed() -> bool:
        return True

    monkeypatch.setattr(automation_worker, "delivery_allowed", allowed)


@pytest.mark.asyncio
async def test_plans_queue_builds_production_runtime_without_injected_worker(monkeypatch):
    worker_ids: list[str] = []
    from greenkube.core.observability import AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP, AUTOMATION_WORKER_UP

    class PullRequestRepository:
        async def reconcile_open_pull_requests(self, *, stale_before, limit):
            raise AssertionError("pending-PR recovery runs in the controller's automation loop (F8)")

    class Plans:
        async def prune_worker_heartbeats(self, *, cutoff):
            raise AssertionError("retention runs in the controller's automation loop (F8)")

        async def list_pending_webhook_deliveries(self, **kwargs):
            raise AssertionError("the webhook outbox is drained by the controller's automation loop (F8)")

    class PlanQueue:
        pull_request_repository = PullRequestRepository()
        plans = Plans()

        async def process_once(self, worker_id: str) -> bool:
            assert AUTOMATION_WORKER_UP._value.get() == 1
            worker_ids.append(worker_id)
            return False

    runtime_calls = 0

    async def create_runtime(**options):
        nonlocal runtime_calls
        runtime_calls += 1
        # Always-on automation (F12): the worker composes without explicit providers and
        # leaves the provider rows to the controller.
        assert options == {"allow_empty": True, "reconcile_providers": False}
        return SimpleNamespace(worker=PlanQueue())

    monkeypatch.setattr(automation_worker, "create_plan_runtime", create_runtime)

    await automation_worker.run_worker(once=True)

    assert runtime_calls == 1
    assert worker_ids
    assert AUTOMATION_WORKER_UP._value.get() == 0
    assert AUTOMATION_WORKER_HEARTBEAT_TIMESTAMP._value.get() > 0


def test_dedicated_worker_only_executes_plan_operations():
    """F8: the optional worker is an executor; housekeeping lives in the controller."""
    for moved in (
        "_process_pending_webhook_deliveries",
        "_PullRequestPollerSupervisor",
        "_create_pr_polling_runtime",
        "_create_branch_cleanup_runtime",
        "_run_startup_maintenance",
    ):
        assert not hasattr(automation_worker, moved), moved


class _StopLoop(BaseException):
    """Escapes the supervised loop, which deliberately survives every ``Exception``."""


@pytest.mark.asyncio
async def test_worker_loop_reuses_one_identity_for_all_claims(monkeypatch):
    worker = cast(PlanWorker, object())
    worker_ids: list[str] = []
    stop_loop = _StopLoop("stop worker loop")

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        assert plan_worker is worker
        assert isinstance(worker_id, str)
        worker_ids.append(worker_id)
        if len(worker_ids) == 3:
            raise stop_loop
        return True

    monkeypatch.setattr(automation_worker, "process_once", process_once)

    with pytest.raises(_StopLoop, match="stop worker loop"):
        await automation_worker._run_worker_loop(poll_seconds=0.1, plan_worker=worker)

    assert len(worker_ids) == 3
    assert len(set(worker_ids)) == 1


@pytest.mark.asyncio
async def test_worker_iteration_failures_are_logged_retried_and_heartbeat(monkeypatch, caplog):
    """D9 / BUG-51: one failed iteration used to end the queue loop silently."""
    worker = cast(PlanWorker, object())
    calls = 0
    heartbeats = 0
    delays: list[float] = []

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError("database is restarting")
        if calls == 4:
            raise _StopLoop()
        return True

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(automation_worker, "process_once", process_once)
    monkeypatch.setattr(automation_worker.asyncio, "sleep", fast_sleep)

    with caplog.at_level("ERROR", logger="greenkube.cli.automation_worker"):
        with pytest.raises(_StopLoop):
            await automation_worker._run_worker_loop(poll_seconds=0.5, plan_worker=worker, heartbeat=heartbeat)

    assert calls == 4
    assert heartbeats == 4
    assert len(delays) == 2 and all(delay > 0 for delay in delays)
    assert "error_code=ConnectionError" in caplog.text
    assert "database is restarting" not in caplog.text


@pytest.mark.asyncio
async def test_worker_once_mode_still_surfaces_iteration_failures(monkeypatch):
    worker = cast(PlanWorker, object())

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        raise ConnectionError("boom")

    monkeypatch.setattr(automation_worker, "process_once", process_once)

    with pytest.raises(ConnectionError):
        await automation_worker._run_worker_loop(poll_seconds=0.1, once=True, plan_worker=worker)


def test_deprecated_queue_flag_accepts_only_plans(monkeypatch):
    runs: list[dict] = []

    async def run_worker(**kwargs):
        runs.append(kwargs)

    monkeypatch.setattr(automation_worker, "run_worker", run_worker)
    runner = CliRunner()

    rejected = runner.invoke(automation_worker.app, ["--once", "--queue", "legacy"])
    accepted = runner.invoke(automation_worker.app, ["--once", "--queue", "plans"])

    assert rejected.exit_code == 2
    assert accepted.exit_code == 0
    assert [run["once"] for run in runs] == [True]


@pytest.mark.asyncio
async def test_plans_queue_safely_idles_when_provider_json_is_absent(monkeypatch):
    async def create_runtime(**_options):
        raise PlanProvidersNotConfiguredError("GITOPS_PROVIDERS_JSON is required.")

    monkeypatch.setattr(automation_worker, "create_plan_runtime", create_runtime)

    assert not await automation_worker.process_once()


@pytest.mark.asyncio
async def test_dedicated_worker_supervises_metrics_listener_without_changing_queue_loop(monkeypatch):
    process_started = asyncio.Event()
    worker_metrics_ports: list[int] = []
    worker = cast(PlanWorker, object())

    class MetricsServer:
        started = True
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0)

    server = MetricsServer()

    def create_metrics_server(port: int):
        worker_metrics_ports.append(port)
        return server

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        assert plan_worker is worker
        assert worker_id
        process_started.set()
        return False

    monkeypatch.setattr(automation_worker, "create_worker_metrics_server", create_metrics_server)
    monkeypatch.setattr(automation_worker, "process_once", process_once)
    await automation_worker.run_worker(
        poll_seconds=10,
        once=True,
        plan_worker=worker,
        metrics_port=8123,
    )

    assert process_started.is_set()
    assert worker_metrics_ports == [8123]
    assert server.should_exit


def test_worker_metrics_port_is_optional_and_bounded(monkeypatch):
    monkeypatch.delenv(automation_worker.WORKER_METRICS_PORT_ENV, raising=False)

    assert automation_worker.parse_worker_metrics_port() is None
    assert automation_worker.parse_worker_metrics_port("8001") == 8001
    monkeypatch.setenv(automation_worker.WORKER_METRICS_PORT_ENV, "8123")
    assert automation_worker.parse_worker_metrics_port() == 8123

    for value in ("", "0", "65536", "port", " 8001"):
        with pytest.raises(ValueError):
            automation_worker.parse_worker_metrics_port(value)


# ------------------------------------------------------------------ delivery gate (F12)
# Tests named test_gate_* use the real gate (see the _delivery_open fixture).


def _capabilities(monkeypatch, rows=(), *, error: Exception | None = None, paused: bool = False) -> datetime:
    from greenkube.storage import automation_capability_repository as capability_module

    class Repository:
        def __init__(self, _db):
            if paused:
                raise AssertionError("a paused worker does not read the capability table")

        async def list_capabilities(self, scope=None):
            if error is not None:
                raise error
            return list(rows)

    monkeypatch.setattr(capability_module, "AutomationCapabilityRepository", Repository)
    monkeypatch.setattr("greenkube.core.db.get_db_manager", lambda: object())
    monkeypatch.setattr("greenkube.core.config.get_config", lambda: SimpleNamespace(automation_delivery_paused=paused))
    return datetime.now(timezone.utc)


def _row(subject: str, state: str, updated_at: datetime | None) -> CapabilityRow:
    return CapabilityRow(SCOPE_GLOBAL, subject, state, {}, updated_at=updated_at)


@pytest.mark.asyncio
async def test_gate_paused_delivery_claims_nothing_but_keeps_the_heartbeat(monkeypatch):
    """pauseDelivery must hold operations approved before a change freeze (F12)."""
    from greenkube.core.observability import AUTOMATION_WORKER_UP

    _capabilities(monkeypatch, paused=True)
    claims: list[str | None] = []
    heartbeats = 0
    sleeps = 0

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        claims.append(worker_id)
        return True

    def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    async def fast_sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise _StopLoop()

    monkeypatch.setattr(automation_worker, "process_once", process_once)
    monkeypatch.setattr(automation_worker.asyncio, "sleep", fast_sleep)

    with pytest.raises(_StopLoop):
        await automation_worker._run_worker_loop(
            poll_seconds=0.1, plan_worker=cast(PlanWorker, object()), heartbeat=heartbeat
        )

    assert claims == []
    assert heartbeats == 3
    assert AUTOMATION_WORKER_UP._value.get() == 0


@pytest.mark.asyncio
async def test_gate_worker_resumes_when_delivery_activates(monkeypatch):
    states = iter([False, True, True])
    claims: list[str | None] = []

    async def gate() -> bool:
        return next(states)

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        claims.append(worker_id)
        if len(claims) == 2:
            raise _StopLoop()
        return True

    async def fast_sleep(delay):
        return None

    monkeypatch.setattr(automation_worker, "process_once", process_once)
    monkeypatch.setattr(automation_worker.asyncio, "sleep", fast_sleep)

    with pytest.raises(_StopLoop):
        await automation_worker._run_worker_loop(
            poll_seconds=0.1, plan_worker=cast(PlanWorker, object()), delivery_gate=gate
        )

    assert len(claims) == 2


@pytest.mark.asyncio
async def test_gate_once_mode_returns_without_claiming_when_the_gate_fails(monkeypatch):
    async def gate() -> bool:
        raise RuntimeError("unreadable")

    async def process_once(*, worker_id=None, plan_worker=None) -> bool:
        raise AssertionError("gated workers never claim")

    monkeypatch.setattr(automation_worker, "process_once", process_once)

    await automation_worker._run_worker_loop(once=True, plan_worker=cast(PlanWorker, object()), delivery_gate=gate)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "age_minutes", "expected"),
    [("active", 1, True), ("dormant", 1, False), ("active", 30, False)],
)
async def test_gate_follows_the_fresh_global_delivery_capability(monkeypatch, state, age_minutes, expected):
    now = datetime.now(timezone.utc)
    rows = [_row("preview", "active", now), _row("delivery", state, now - timedelta(minutes=age_minutes))]
    _capabilities(monkeypatch, rows)

    assert await automation_worker.delivery_allowed(now=now) is expected


@pytest.mark.asyncio
async def test_gate_fails_closed(monkeypatch):
    now = _capabilities(monkeypatch, error=ConnectionError("database is restarting"))
    assert await automation_worker.delivery_allowed(now=now) is False

    _capabilities(monkeypatch, [])
    assert await automation_worker.delivery_allowed(now=now) is False

    _capabilities(monkeypatch, [_row("delivery", "active", None)])
    assert await automation_worker.delivery_allowed(now=now) is False

    _capabilities(monkeypatch, [_row("delivery", "active", now)], paused=True)
    assert await automation_worker.delivery_allowed(now=now) is False
