from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from greenkube.core.leader_election import KubernetesLeaseElector, process_identity

NOW = datetime(2026, 10, 23, 0, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class _FakeCoordinationApi:
    def __init__(
        self,
        lease: client.V1Lease | None = None,
        *,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.lease = deepcopy(lease)
        self.errors = dict(errors or {})
        self.create_requests: list[client.V1Lease] = []
        self.replace_requests: list[client.V1Lease] = []

    async def read_namespaced_lease(self, *, name: str, namespace: str) -> client.V1Lease:
        assert name == "controller-leader"
        assert namespace == "greenkube"
        if "read" in self.errors:
            raise self.errors["read"]
        if self.lease is None:
            raise ApiException(status=404, reason="Not Found")
        return deepcopy(self.lease)

    async def create_namespaced_lease(self, *, namespace: str, body: client.V1Lease) -> client.V1Lease:
        assert namespace == "greenkube"
        self.create_requests.append(deepcopy(body))
        if "create" in self.errors:
            raise self.errors["create"]
        if self.lease is not None:
            raise ApiException(status=409, reason="Already Exists")
        self.lease = deepcopy(body)
        self.lease.metadata.resource_version = "1"
        return deepcopy(self.lease)

    async def replace_namespaced_lease(
        self,
        *,
        name: str,
        namespace: str,
        body: client.V1Lease,
    ) -> client.V1Lease:
        assert name == "controller-leader"
        assert namespace == "greenkube"
        self.replace_requests.append(deepcopy(body))
        if "replace" in self.errors:
            raise self.errors["replace"]
        if self.lease is None or body.metadata.resource_version != self.lease.metadata.resource_version:
            raise ApiException(status=409, reason="Conflict")
        self.lease = deepcopy(body)
        self.lease.metadata.resource_version = str(int(self.lease.metadata.resource_version) + 1)
        return deepcopy(self.lease)


def _lease(
    holder: str | None,
    *,
    renew_time: datetime | None = NOW,
    duration: int = 30,
    resource_version: str | None = "7",
    transitions: int | None = 0,
) -> client.V1Lease:
    return client.V1Lease(
        metadata=client.V1ObjectMeta(
            name="controller-leader",
            namespace="greenkube",
            resource_version=resource_version,
            labels={"app.kubernetes.io/component": "controller"},
        ),
        spec=client.V1LeaseSpec(
            holder_identity=holder,
            lease_duration_seconds=duration,
            acquire_time=NOW - timedelta(seconds=10) if holder else None,
            renew_time=renew_time,
            lease_transitions=transitions,
        ),
    )


def _elector(api: _FakeCoordinationApi, clock: _Clock | None = None) -> KubernetesLeaseElector:
    return KubernetesLeaseElector(
        api=api,
        name="controller-leader",
        namespace="greenkube",
        identity="controller-pod:1",
        clock=clock or _Clock(),
    )


@pytest.mark.asyncio
async def test_acquires_missing_lease_then_renews_with_resource_version():
    api = _FakeCoordinationApi()
    clock = _Clock()
    elector = _elector(api, clock)

    assert await elector.try_acquire_or_renew()
    assert elector.is_leader
    assert len(api.create_requests) == 1
    assert api.lease is not None
    assert api.lease.spec.holder_identity == "controller-pod:1"
    assert api.lease.spec.lease_transitions == 0

    acquire_time = api.lease.spec.acquire_time
    clock.advance(4)

    assert await elector.try_acquire_or_renew()
    assert api.replace_requests[0].metadata.resource_version == "1"
    assert api.lease.metadata.resource_version == "2"
    assert api.lease.spec.holder_identity == "controller-pod:1"
    assert api.lease.spec.acquire_time == acquire_time
    assert api.lease.spec.renew_time == clock.current


@pytest.mark.asyncio
async def test_contention_does_not_take_over_an_unexpired_lease():
    api = _FakeCoordinationApi(_lease("another-controller", renew_time=NOW - timedelta(seconds=10)))
    elector = _elector(api)

    assert not await elector.try_acquire_or_renew()
    assert not elector.is_leader
    assert not api.replace_requests
    assert api.lease is not None
    assert api.lease.spec.holder_identity == "another-controller"


@pytest.mark.asyncio
async def test_acquires_chart_created_lease_with_empty_holder():
    api = _FakeCoordinationApi(_lease(None, renew_time=None, resource_version="9", transitions=None))
    elector = _elector(api)

    assert await elector.try_acquire_or_renew()
    assert api.replace_requests[0].metadata.resource_version == "9"
    assert api.lease is not None
    assert api.lease.spec.holder_identity == "controller-pod:1"
    assert api.lease.spec.lease_transitions == 1


@pytest.mark.asyncio
async def test_expired_lease_is_taken_over_with_compare_and_swap():
    expired = _lease(
        "previous-controller",
        renew_time=NOW - timedelta(seconds=31),
        resource_version="42",
        transitions=3,
    )
    api = _FakeCoordinationApi(expired)
    elector = _elector(api)

    assert await elector.try_acquire_or_renew()
    assert elector.is_leader
    assert api.replace_requests[0].metadata.resource_version == "42"
    assert api.replace_requests[0].metadata.labels == {"app.kubernetes.io/component": "controller"}
    assert api.lease is not None
    assert api.lease.spec.holder_identity == "controller-pod:1"
    assert api.lease.spec.lease_transitions == 4
    assert api.lease.spec.acquire_time == NOW


@pytest.mark.asyncio
async def test_competing_resource_version_change_prevents_lease_takeover():
    api = _FakeCoordinationApi(
        _lease("previous-controller", renew_time=NOW - timedelta(seconds=31), resource_version="42")
    )
    replace = api.replace_namespaced_lease

    async def competing_update(*, name: str, namespace: str, body: client.V1Lease) -> client.V1Lease:
        assert api.lease is not None
        api.lease.metadata.resource_version = "43"
        return await replace(name=name, namespace=namespace, body=body)

    api.replace_namespaced_lease = competing_update
    elector = _elector(api)

    assert not await elector.try_acquire_or_renew()
    assert api.replace_requests[0].metadata.resource_version == "42"
    assert api.lease is not None
    assert api.lease.metadata.resource_version == "43"
    assert not elector.is_leader


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "status", "lease"),
    [
        ("read", 503, None),
        ("create", 403, None),
        ("create", 409, None),
        ("replace", 403, _lease("previous-controller", renew_time=NOW - timedelta(seconds=31))),
        ("replace", 409, _lease("controller-pod:1")),
    ],
)
async def test_api_errors_and_resource_version_conflicts_fail_closed(operation, status, lease):
    api = _FakeCoordinationApi(
        lease,
        errors={operation: ApiException(status=status, reason="API failure")},
    )
    elector = _elector(api)

    assert not await elector.try_acquire_or_renew()
    assert not elector.is_leader


@pytest.mark.asyncio
async def test_unavailable_kubernetes_api_fails_closed():
    async def no_api():
        return None

    elector = KubernetesLeaseElector(
        api_provider=no_api,
        name="controller-leader",
        namespace="greenkube",
        identity="controller-pod:1",
    )

    assert not await elector.try_acquire_or_renew()
    assert not elector.is_leader


@pytest.mark.asyncio
async def test_missing_resource_version_never_uses_an_unconditional_update():
    api = _FakeCoordinationApi(
        _lease("previous-controller", renew_time=NOW - timedelta(seconds=31), resource_version=None)
    )
    elector = _elector(api)

    assert not await elector.try_acquire_or_renew()
    assert not api.replace_requests
    assert not elector.is_leader


@pytest.mark.asyncio
async def test_local_renewal_deadline_fails_closed_before_lease_expiry():
    api = _FakeCoordinationApi()
    clock = _Clock()
    elector = _elector(api, clock)

    assert await elector.try_acquire_or_renew()
    clock.advance(11)

    assert not elector.is_leader


@pytest.mark.asyncio
async def test_failed_renewal_relinquishes_local_leadership():
    api = _FakeCoordinationApi()
    clock = _Clock()
    elector = _elector(api, clock)
    assert await elector.try_acquire_or_renew()
    assert elector.is_leader

    clock.advance(1)
    api.errors["replace"] = ApiException(status=503, reason="Service Unavailable")

    assert not await elector.try_acquire_or_renew()
    assert not elector.is_leader


def test_process_identity_is_stable_for_a_host_and_process():
    assert process_identity(hostname="controller-pod", process_id=17) == "controller-pod:17"
    assert process_identity(hostname="controller-pod", process_id=17) == "controller-pod:17"


# ---------------------------------------------------------------------------
# Leader fencing (A14, BUG-13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fencing_token_is_the_lease_generation_and_survives_own_renewals():
    from greenkube.core.leader_election import LeaderFence

    api = _FakeCoordinationApi()
    clock = _Clock()
    elector = _elector(api, clock)
    assert elector.fencing_token is None

    assert await elector.try_acquire_or_renew()
    fence = LeaderFence(elector)
    assert fence.token == 0

    clock.advance(4)
    assert await elector.try_acquire_or_renew()
    fence.check()  # renewing our own Lease keeps the generation


@pytest.mark.asyncio
async def test_fence_rejects_commit_after_local_lease_deadline():
    from greenkube.core.leader_election import LeaderFence, LeadershipLostError

    api = _FakeCoordinationApi()
    clock = _Clock()
    elector = _elector(api, clock)
    assert await elector.try_acquire_or_renew()
    fence = LeaderFence(elector)

    clock.advance(11)  # past the local renew deadline: another holder may take over soon

    assert not fence.valid
    with pytest.raises(LeadershipLostError):
        fence.check("advance watermark")


@pytest.mark.asyncio
async def test_fence_rejects_commit_when_another_holder_owned_the_lease_in_between():
    from greenkube.core.leader_election import LeaderFence, LeadershipLostError

    api = _FakeCoordinationApi()
    clock = _Clock()
    elector = _elector(api, clock)
    assert await elector.try_acquire_or_renew()
    fence = LeaderFence(elector)

    # Another controller takes the expired Lease, then this process takes it back.
    assert api.lease is not None
    api.lease.spec.holder_identity = "other-controller:9"
    api.lease.spec.lease_transitions = 1
    api.lease.spec.renew_time = clock.current
    clock.advance(31)
    assert await elector.try_acquire_or_renew()

    assert elector.is_leader and elector.fencing_token == 2
    with pytest.raises(LeadershipLostError):
        fence.check()


@pytest.mark.asyncio
async def test_leader_gate_scopes_a_fence_for_the_running_job():
    from greenkube.core.leader_election import LeaderGate, current_leader_fence, leader_fence_scope

    api = _FakeCoordinationApi()
    elector = _elector(api)
    assert await elector.try_acquire_or_renew()
    gate = LeaderGate(elector)

    assert gate() is True
    assert not current_leader_fence().enforced
    with leader_fence_scope(gate.fence()) as fence:
        assert current_leader_fence() is fence
        assert fence.enforced and fence.token == 0
    assert not current_leader_fence().enforced


# -- Shutdown hand-over: a stopping leader releases the Lease ------------------------


@pytest.mark.asyncio
async def test_stopping_leader_releases_the_lease_so_a_standby_takes_over_at_once():
    import asyncio

    api = _FakeCoordinationApi()
    clock = _Clock()
    leader = _elector(api, clock)
    stop = asyncio.Event()
    stop.set()
    assert await leader.try_acquire_or_renew()
    fence = leader.fencing_token
    assert fence is not None

    held = await leader.run(stop)

    # run() never releases by itself: leader work may still be unwinding (the caller
    # releases once it has stopped), so the Lease still names this replica.
    assert held is True
    assert not leader.is_leader
    assert api.lease is not None
    assert api.lease.spec.holder_identity == "controller-pod:1"

    assert await leader.release() is True
    assert api.lease.spec.holder_identity is None
    standby = KubernetesLeaseElector(
        api=api, name="controller-leader", namespace="greenkube", identity="controller-pod:2", clock=clock
    )
    # No wait for the 30 s expiry, and the generation moves on (old fences are void).
    assert await standby.try_acquire_or_renew()
    assert standby.fencing_token == fence + 1


@pytest.mark.asyncio
async def test_release_never_clears_a_lease_held_by_another_replica():
    api = _FakeCoordinationApi(_lease("another-controller"))
    elector = _elector(api)

    assert await elector.release() is False
    assert api.lease is not None
    assert api.lease.spec.holder_identity == "another-controller"
    assert api.replace_requests == []


@pytest.mark.asyncio
async def test_release_errors_leave_the_lease_to_expire():
    api = _FakeCoordinationApi()
    elector = _elector(api)
    assert await elector.try_acquire_or_renew()
    api.errors["replace"] = ApiException(status=500, reason="boom")

    assert await elector.release() is False
    assert not elector.is_leader
    assert api.lease is not None
    assert api.lease.spec.holder_identity == "controller-pod:1"


@pytest.mark.asyncio
async def test_standby_shutdown_does_not_touch_the_lease():
    import asyncio

    api = _FakeCoordinationApi(_lease("another-controller"))
    elector = _elector(api)
    stop = asyncio.Event()
    stop.set()

    assert await elector.run(stop) is False

    assert api.replace_requests == []
