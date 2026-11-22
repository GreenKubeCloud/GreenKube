"""Controller loop registry (roadmap M8): leader gating, isolation, cadence, telemetry."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.leader_election import LeaderGate, current_leader_fence
from greenkube.core.loops import LoopRegistry, LoopSpec, report_loop_failure
from greenkube.core.observability import REGISTRY, generate_latest_observability


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def _noop() -> None:
    return None


# -- declaration --------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "Bad Name"},
        {"name": ""},
        {"interval_seconds": 0},
        {"interval_seconds": float("inf")},
        {"jitter": 1.0},
        {"timeout_seconds": -1},
        {"max_backoff_multiplier": 0},
        {"align_offset_seconds": -5},
    ],
)
def test_loop_spec_rejects_invalid_declarations(kwargs):
    values = {"name": "valid-loop", "func": _noop, "interval_seconds": 60}
    values.update(kwargs)
    with pytest.raises(ValueError):
        LoopSpec(**values)


def test_registry_rejects_duplicate_loop_names():
    registry = LoopRegistry()
    registry.register(LoopSpec("collection", _noop, 60))
    with pytest.raises(ValueError):
        registry.register(LoopSpec("collection", _noop, 60))


# -- leader gating ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_leader_runs_leader_only_loops():
    state = {"leader": False}
    calls: list[str] = []

    async def leader_job():
        calls.append("leader")

    async def every_replica_job():
        calls.append("any")

    registry = LoopRegistry(leader=lambda: state["leader"])
    registry.register(LoopSpec("leader-job", leader_job, 60))
    registry.register(LoopSpec("any-replica-job", every_replica_job, 60, leader_only=False))
    skipped_before = _sample("greenkube_loop_runs_total", {"loop": "leader-job", "outcome": "skipped"})

    assert await registry.run_once("leader-job") == "skipped"
    assert await registry.run_once("any-replica-job") == "success"
    assert calls == ["any"]
    assert registry.status("leader-job").state == "standby"
    assert _sample("greenkube_loop_runs_total", {"loop": "leader-job", "outcome": "skipped"}) == skipped_before + 1
    assert registry.snapshot()["leader"] is False

    state["leader"] = True
    assert await registry.run_once("leader-job") == "success"
    assert calls == ["any", "leader"]
    assert registry.snapshot()["leader"] is True


@pytest.mark.asyncio
async def test_standby_hook_runs_only_when_a_leader_only_tick_is_skipped():
    state = {"leader": True}
    standby_calls = 0

    def on_standby():
        nonlocal standby_calls
        standby_calls += 1
        raise RuntimeError("hook errors are logged, not raised")

    registry = LoopRegistry(leader=lambda: state["leader"])
    registry.register(LoopSpec("with-hook", _noop, 60, on_standby=on_standby))

    assert await registry.run_once("with-hook") == "success"
    assert standby_calls == 0
    state["leader"] = False
    assert await registry.run_once("with-hook") == "skipped"
    assert standby_calls == 1


@pytest.mark.asyncio
async def test_without_leader_election_this_process_runs_every_loop():
    calls = 0

    async def job():
        nonlocal calls
        calls += 1

    registry = LoopRegistry()
    registry.register(LoopSpec("leader-job", job, 60))

    assert await registry.run_once("leader-job") == "success"
    assert calls == 1


@pytest.mark.asyncio
async def test_leader_only_ticks_run_inside_a_lease_fence():
    class _Elector:
        is_leader = True
        fencing_token = 7

    seen = []

    async def job():
        seen.append(current_leader_fence())

    registry = LoopRegistry(leader=LeaderGate(_Elector()))  # type: ignore[arg-type]
    registry.register(LoopSpec("fenced-job", job, 60))
    await registry.run_once("fenced-job")

    assert seen[0].enforced and seen[0].token == 7
    assert not current_leader_fence().enforced


@pytest.mark.asyncio
async def test_a_failing_leader_predicate_pauses_leader_only_loops():
    def broken():
        raise RuntimeError("clock failed")

    registry = LoopRegistry(leader=broken)
    registry.register(LoopSpec("leader-job", _noop, 60))

    assert await registry.run_once("leader-job") == "skipped"


# -- isolation and outcomes ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_loop_does_not_stop_the_others():
    healthy_runs = 0
    failing_runs = 0

    async def healthy():
        nonlocal healthy_runs
        healthy_runs += 1

    async def failing():
        nonlocal failing_runs
        failing_runs += 1
        raise ConnectionError("database restarting")

    registry = LoopRegistry()
    registry.register(LoopSpec("healthy-loop", healthy, 0.02, run_immediately=True, jitter=0))
    registry.register(LoopSpec("failing-loop", failing, 0.02, run_immediately=True, jitter=0, max_backoff_multiplier=1))
    failures_before = _sample("greenkube_loop_runs_total", {"loop": "failing-loop", "outcome": "failure"})
    registry.start()
    try:
        for _ in range(200):
            if healthy_runs >= 5 and failing_runs >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await registry.stop()

    assert healthy_runs >= 5
    assert failing_runs >= 3
    failing = registry.status("failing-loop")
    assert failing.last_error_code == "ConnectionError"
    assert failing.consecutive_failures >= 3
    assert registry.status("healthy-loop").consecutive_failures == 0
    assert _sample("greenkube_loop_runs_total", {"loop": "failing-loop", "outcome": "failure"}) >= failures_before + 3
    assert _sample("greenkube_loop_consecutive_failures", {"loop": "healthy-loop"}) == 0


@pytest.mark.asyncio
async def test_a_wedged_tick_times_out_and_the_loop_continues():
    async def wedged():
        await asyncio.sleep(10)

    registry = LoopRegistry()
    registry.register(LoopSpec("wedged-loop", wedged, 60, timeout_seconds=0.05))

    assert await registry.run_once("wedged-loop") == "timeout"
    status = registry.status("wedged-loop")
    assert status.timeouts == 1 and status.last_error_code == "timeout"
    assert status.state == "failing"


@pytest.mark.asyncio
async def test_a_timeout_raised_by_the_job_itself_is_a_failure_not_a_registry_timeout():
    async def provider_call_timing_out():
        raise TimeoutError("provider read timeout")

    registry = LoopRegistry()
    registry.register(LoopSpec("own-timeout", provider_call_timing_out, 60, timeout_seconds=30))

    assert await registry.run_once("own-timeout") == "failure"
    status = registry.status("own-timeout")
    assert status.timeouts == 0
    assert status.last_error_code == "TimeoutError"


@pytest.mark.asyncio
async def test_a_stray_cancellation_inside_a_job_fails_the_tick_and_the_loop_keeps_running():
    runs = 0

    async def job():
        nonlocal runs
        runs += 1
        if runs == 1:
            inner = asyncio.get_running_loop().create_future()
            inner.cancel()
            await inner  # raises CancelledError although nobody cancelled the loop task

    registry = LoopRegistry()
    registry.register(LoopSpec("stray-cancel", job, 0.02, run_immediately=True, jitter=0, max_backoff_multiplier=1))
    registry.start()
    try:
        for _ in range(200):
            if runs >= 3:
                break
            await asyncio.sleep(0.01)
        status = registry.status("stray-cancel")
        assert runs >= 3
        assert status.restarts == 0
        assert status.state != "stopped"
        assert status.failures >= 1
    finally:
        await registry.stop()
    assert registry.status("stray-cancel").state == "stopped"


@pytest.mark.asyncio
async def test_stopping_the_registry_still_cancels_a_running_tick():
    started = asyncio.Event()

    async def long_job():
        started.set()
        await asyncio.sleep(60)

    registry = LoopRegistry()
    registry.register(LoopSpec("long-job", long_job, 60, run_immediately=True))
    registry.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.wait_for(registry.stop(), timeout=2)

    assert registry.status("long-job").state == "stopped"
    assert registry.status("long-job").failures == 0


@pytest.mark.asyncio
async def test_a_job_can_mark_its_tick_failed_without_raising():
    async def self_handling_job():
        try:
            raise RuntimeError("prometheus unreachable")
        except RuntimeError as error:
            report_loop_failure(error)

    registry = LoopRegistry()
    registry.register(LoopSpec("self-handling", self_handling_job, 60))

    assert await registry.run_once("self-handling") == "failure"
    assert registry.status("self-handling").last_error_code == "RuntimeError"
    # Outside a tick the helper is a no-op.
    report_loop_failure("ignored")


@pytest.mark.asyncio
async def test_a_crashed_loop_task_is_reported_and_restarted(monkeypatch):
    import greenkube.core.loops as loops_module

    monkeypatch.setattr(loops_module, "LOOP_RESTART_DELAY_SECONDS", 0.01)
    registry = LoopRegistry()
    registry.register(LoopSpec("fragile", _noop, 0.02, run_immediately=True, jitter=0))
    crashes = 0
    real_next_delay = registry._next_delay

    def next_delay(spec, outcome, duration):
        nonlocal crashes
        if crashes == 0:
            crashes += 1
            raise RuntimeError("scheduler bug")
        return real_next_delay(spec, outcome, duration)

    monkeypatch.setattr(registry, "_next_delay", next_delay)
    registry.start()
    try:
        for _ in range(100):
            status = registry.status("fragile")
            if status.restarts and status.runs >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await registry.stop()

    status = registry.status("fragile")
    assert status.restarts == 1
    assert status.runs >= 2


# -- cadence -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raised_failures_back_off_exponentially_up_to_the_cap():
    async def raising():
        raise ConnectionError("database restarting")

    registry = LoopRegistry(rng=lambda: 0.5)
    spec = registry.register(LoopSpec("backoff-loop", raising, 10, max_backoff_multiplier=8))

    assert registry._next_delay(spec, "success", 2.0) == pytest.approx(8.0)
    assert await registry.run_once("backoff-loop") == "failure"
    assert registry._next_delay(spec, "failure", 0.0) == pytest.approx(20.0)
    for _ in range(4):
        await registry.run_once("backoff-loop")
    assert registry.status("backoff-loop").consecutive_failures == 5
    assert registry._next_delay(spec, "failure", 0.0) == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_reported_failures_keep_the_job_cadence_but_are_counted():
    """A job that handles its own errors keeps its interval (no registry backoff)."""

    async def self_handling_job():
        try:
            raise ConnectionError("electricity maps zone unavailable")
        except ConnectionError as error:
            report_loop_failure(error)

    registry = LoopRegistry(rng=lambda: 0.5)
    spec = registry.register(LoopSpec("reported-failure", self_handling_job, 3600, max_backoff_multiplier=8))
    failures_before = _sample("greenkube_loop_runs_total", {"loop": "reported-failure", "outcome": "failure"})

    for _ in range(4):
        assert await registry.run_once("reported-failure") == "failure"
        assert registry._next_delay(spec, "failure", 0.0) == pytest.approx(3600)

    status = registry.status("reported-failure")
    assert status.consecutive_failures == 4
    assert status.state == "failing"
    assert _sample("greenkube_loop_runs_total", {"loop": "reported-failure", "outcome": "failure"}) == (
        failures_before + 4
    )
    assert _sample("greenkube_loop_consecutive_failures", {"loop": "reported-failure"}) == 4


@pytest.mark.asyncio
async def test_a_reported_failure_resets_the_backoff_of_earlier_raised_failures():
    raise_next = True

    async def job():
        if raise_next:
            raise ConnectionError("down")
        report_loop_failure("partial")

    registry = LoopRegistry(rng=lambda: 0.5)
    spec = registry.register(LoopSpec("mixed", job, 10, max_backoff_multiplier=8))

    await registry.run_once("mixed")
    await registry.run_once("mixed")
    assert registry._next_delay(spec, "failure", 0.0) == pytest.approx(40.0)
    raise_next = False
    await registry.run_once("mixed")
    assert registry._next_delay(spec, "failure", 0.0) == pytest.approx(10.0)


def test_standby_loops_recheck_the_lease_sooner_than_their_interval():
    registry = LoopRegistry(rng=lambda: 0.5)
    quick = registry.register(LoopSpec("quick-takeover", _noop, 300, standby_recheck_seconds=2))
    plain = registry.register(LoopSpec("plain", _noop, 300))

    assert registry._next_delay(quick, "skipped", 0.0) == 2
    assert registry._next_delay(plain, "skipped", 0.0) == pytest.approx(300)


@pytest.mark.asyncio
async def test_aligned_loops_tick_on_wall_clock_boundaries():
    sleeps: list[float] = []
    calls = 0

    async def job():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("one bad window")

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    registry = LoopRegistry(wall_time=lambda: 1_000_030.0, sleep=fake_sleep)
    spec = registry.register(LoopSpec("collection", job, 300, align_offset_seconds=60))
    with pytest.raises(asyncio.CancelledError):
        await registry._run(spec)

    # 1_000_030 - 60 s offset is 70 s past a 300 s boundary: next tick in 230 s, every
    # time, even after the failed window (no backoff on aligned loops).
    assert sleeps == [230.0, 230.0, 230.0]
    assert calls == 2


# -- health and telemetry -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_tracks_heartbeats_and_crashes():
    now = datetime.now(timezone.utc)
    clock = {"now": now}
    registry = LoopRegistry(clock=lambda: clock["now"])
    registry.register(LoopSpec("hourly", _noop, 3600, max_backoff_multiplier=1, jitter=0))

    # Registered but never started: not ready.
    assert not registry.ready()
    await registry.run_once("hourly")
    assert registry.ready()

    clock["now"] = now + timedelta(seconds=3600 + 59)
    assert registry.ready()
    clock["now"] = now + timedelta(seconds=3600 + 61)
    assert not registry.ready()
    snapshot = registry.snapshot()
    assert snapshot["ready"] is False
    assert snapshot["loops"][0]["name"] == "hourly"
    assert snapshot["loops"][0]["ready"] is False

    await registry.run_once("hourly")
    assert registry.ready()
    registry.status("hourly").state = "crashed"
    assert not registry.ready()


@pytest.mark.asyncio
async def test_loop_metrics_are_exported():
    async def job():
        await asyncio.sleep(0)

    registry = LoopRegistry()
    registry.register(LoopSpec("exported-loop", job, 60))
    await registry.run_once("exported-loop")

    body = generate_latest_observability().decode()
    assert 'greenkube_loop_runs_total{loop="exported-loop",outcome="success"}' in body
    assert 'greenkube_loop_duration_seconds_count{loop="exported-loop"}' in body
    assert 'greenkube_loop_last_success_timestamp_seconds{loop="exported-loop"}' in body
    assert 'greenkube_loop_consecutive_failures{loop="exported-loop"} 0.0' in body
    assert "greenkube_controller_leader 1.0" in body
    assert _sample("greenkube_loop_last_success_timestamp_seconds", {"loop": "exported-loop"}) > 0
    snapshot = registry.snapshot()["loops"][0]
    assert snapshot["last_outcome"] == "success" and snapshot["last_success_at"].endswith("Z")
