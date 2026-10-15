"""Kubernetes Lease-based coordination for controller work."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import socket
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from kubernetes_asyncio import client

DEFAULT_LEADER_ELECTION_LEASE_DURATION_SECONDS = 30
DEFAULT_LEADER_ELECTION_RENEW_DEADLINE_SECONDS = 10
DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS = 2
_LOCAL_RENEW_DEADLINE_FRACTION = 2 / 3

logger = logging.getLogger(__name__)


def process_identity(*, hostname: str | None = None, process_id: int | None = None) -> str:
    """Return one stable identity for this process for its entire Lease tenure."""
    selected_hostname = hostname or os.getenv("POD_NAME") or os.getenv("HOSTNAME") or socket.gethostname()
    if not isinstance(selected_hostname, str) or not selected_hostname.strip():
        raise ValueError("A stable hostname is required for Kubernetes leader election.")
    selected_process_id = os.getpid() if process_id is None else process_id
    if type(selected_process_id) is not int or selected_process_id <= 0:
        raise ValueError("process_id must be a positive integer.")
    return f"{selected_hostname.strip()}:{selected_process_id}"


class KubernetesLeaseElector:
    """Acquire, renew, and observe a namespaced Kubernetes Lease with CAS updates."""

    def __init__(
        self,
        *,
        name: str,
        namespace: str,
        identity: str,
        api: Any | None = None,
        api_provider: Callable[[], Awaitable[Any | None]] | None = None,
        lease_duration_seconds: int = DEFAULT_LEADER_ELECTION_LEASE_DURATION_SECONDS,
        retry_period_seconds: int | float = DEFAULT_LEADER_ELECTION_RETRY_PERIOD_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("Lease name must be a non-empty, trimmed string.")
        if not isinstance(namespace, str) or not namespace or namespace != namespace.strip():
            raise ValueError("Lease namespace must be a non-empty, trimmed string.")
        if not isinstance(identity, str) or not identity or identity != identity.strip():
            raise ValueError("Lease identity must be a non-empty, trimmed string.")
        if api is not None and api_provider is not None:
            raise ValueError("Provide either api or api_provider, not both.")
        if type(lease_duration_seconds) is not int or lease_duration_seconds <= 0:
            raise ValueError("lease_duration_seconds must be a positive integer.")
        if (
            isinstance(retry_period_seconds, bool)
            or not isinstance(retry_period_seconds, (int, float))
            or not math.isfinite(retry_period_seconds)
            or retry_period_seconds <= 0
            or retry_period_seconds >= lease_duration_seconds
        ):
            raise ValueError("retry_period_seconds must be positive and shorter than the Lease duration.")

        self.name = name
        self.namespace = namespace
        self.identity = identity
        self.lease_duration_seconds = lease_duration_seconds
        self.retry_period_seconds = float(retry_period_seconds)
        self._api = api
        self._api_provider = api_provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._leader = False
        self._valid_until: datetime | None = None
        self._attempt_lock = asyncio.Lock()

    @property
    def is_leader(self) -> bool:
        """Whether the latest successful Lease write remains within its safe deadline."""
        if self._leader and self._valid_until is not None:
            try:
                if self._now() >= self._valid_until:
                    self._lose_leadership()
            except Exception:
                self._lose_leadership()
                logger.warning("Leader election clock failed; controller work is disabled.")
        return self._leader

    async def try_acquire_or_renew(self) -> bool:
        """Attempt one read/CAS cycle; every API or resource-version error fails closed."""
        async with self._attempt_lock:
            try:
                api = await self._get_api()
                if api is None:
                    self._lose_leadership()
                    logger.warning(
                        "Kubernetes Lease election is unavailable for %s/%s; controller work is disabled.",
                        self.namespace,
                        self.name,
                    )
                    return False

                try:
                    lease = await api.read_namespaced_lease(name=self.name, namespace=self.namespace)
                except Exception as error:
                    if _api_status(error) == 404:
                        return await self._create_lease(api)
                    self._lose_leadership()
                    self._log_api_error("read", error)
                    return False

                return await self._renew_or_take_over(api, lease)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._lose_leadership()
                self._log_api_error("request", error)
                return False

    async def run(self, stop_event: asyncio.Event) -> None:
        """Continuously renew or contend until stopped."""
        try:
            while not stop_event.is_set():
                await self.try_acquire_or_renew()
                if stop_event.is_set():
                    return
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.retry_period_seconds)
                except TimeoutError:
                    continue
        finally:
            self._lose_leadership()

    async def _get_api(self) -> Any | None:
        if self._api_provider is not None:
            return await self._api_provider()
        return self._api

    async def _create_lease(self, api: Any) -> bool:
        now = self._now()
        lease = client.V1Lease(
            metadata=client.V1ObjectMeta(name=self.name, namespace=self.namespace),
            spec=client.V1LeaseSpec(
                holder_identity=self.identity,
                lease_duration_seconds=self.lease_duration_seconds,
                acquire_time=now,
                renew_time=now,
                lease_transitions=0,
            ),
        )
        try:
            await api.create_namespaced_lease(namespace=self.namespace, body=lease)
        except Exception as error:
            self._lose_leadership()
            self._log_api_error("create", error)
            return False
        self._confirm_leadership(now, self.lease_duration_seconds)
        return True

    async def _renew_or_take_over(self, api: Any, lease: Any) -> bool:
        metadata = getattr(lease, "metadata", None)
        resource_version = getattr(metadata, "resource_version", None)
        if not isinstance(resource_version, str) or not resource_version.strip():
            self._lose_leadership()
            logger.warning(
                "Kubernetes Lease %s/%s has no resourceVersion; controller work is disabled.",
                self.namespace,
                self.name,
            )
            return False

        spec = getattr(lease, "spec", None)
        raw_holder = getattr(spec, "holder_identity", None)
        if raw_holder is not None and not isinstance(raw_holder, str):
            self._lose_leadership()
            logger.warning(
                "Kubernetes Lease %s/%s has an invalid holder identity; controller work is disabled.",
                self.namespace,
                self.name,
            )
            return False
        holder = raw_holder or ""
        now = self._now()
        same_holder = holder == self.identity
        if holder and not same_holder and not _lease_expired(spec, now):
            self._lose_leadership()
            return False

        duration = _positive_seconds(getattr(spec, "lease_duration_seconds", None))
        if duration is None:
            if holder and not same_holder:
                self._lose_leadership()
                return False
            duration = self.lease_duration_seconds

        write_time = self._now()
        body = deepcopy(lease)
        body.metadata.resource_version = resource_version
        if body.spec is None:
            body.spec = client.V1LeaseSpec()
        previous_acquire_time = _lease_datetime(getattr(spec, "acquire_time", None))
        transitions = _nonnegative_int(getattr(spec, "lease_transitions", None)) or 0
        if same_holder:
            acquire_time = previous_acquire_time or write_time
        else:
            acquire_time = write_time
            transitions += 1

        body.spec.holder_identity = self.identity
        body.spec.lease_duration_seconds = duration
        body.spec.acquire_time = acquire_time
        body.spec.renew_time = write_time
        body.spec.lease_transitions = transitions
        try:
            await api.replace_namespaced_lease(name=self.name, namespace=self.namespace, body=body)
        except Exception as error:
            self._lose_leadership()
            self._log_api_error("replace", error)
            return False

        self._confirm_leadership(write_time, duration)
        return True

    def _confirm_leadership(self, renewed_at: datetime, duration_seconds: int) -> None:
        self._leader = True
        safe_deadline = min(
            duration_seconds * _LOCAL_RENEW_DEADLINE_FRACTION,
            DEFAULT_LEADER_ELECTION_RENEW_DEADLINE_SECONDS,
        )
        self._valid_until = renewed_at + timedelta(seconds=safe_deadline)

    def _lose_leadership(self) -> None:
        self._leader = False
        self._valid_until = None

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Leader election clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)

    def _log_api_error(self, operation: str, error: Exception) -> None:
        status = _api_status(error)
        if status == 409:
            logger.debug(
                "Kubernetes Lease %s/%s changed during %s; this controller is not the lease holder.",
                self.namespace,
                self.name,
                operation,
            )
            return
        logger.warning(
            "Kubernetes Lease %s/%s %s failed (status=%s); controller work is disabled.",
            self.namespace,
            self.name,
            operation,
            status if status is not None else "unknown",
        )


def _lease_expired(spec: Any, now: datetime) -> bool:
    duration = _positive_seconds(getattr(spec, "lease_duration_seconds", None))
    if duration is None:
        return False
    raw_renew_time = getattr(spec, "renew_time", None)
    if raw_renew_time is None:
        raw_renew_time = getattr(spec, "acquire_time", None)
    renewed_at = _lease_datetime(raw_renew_time)
    if renewed_at is None:
        return False
    return renewed_at + timedelta(seconds=duration) <= now


def _lease_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    return None


def _positive_seconds(value: Any) -> int | None:
    if type(value) is not int or value <= 0:
        return None
    return value


def _nonnegative_int(value: Any) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _api_status(error: Exception) -> int | None:
    status = getattr(error, "status", None)
    return status if type(status) is int else None
