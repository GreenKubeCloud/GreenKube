"""The default credentials Secret read through the Kubernetes API (roadmap F12 follow-up).

The chart mounts ``greenkube-git-credentials`` as an optional projected volume.
The kubelet refreshes such a volume only on its sync period (73 s on kind, up to
several minutes elsewhere), so creating the Secret activated delivery after one
to two minutes. GreenKube now also reads that one Secret from the Kubernetes API:

* ``get`` on the single ``resourceName`` (namespaced Role, never ``list``);
* ``watch`` with ``fieldSelector=metadata.name=<name>`` in the controller, so a
  created, changed or deleted Secret is seen within seconds; the readiness
  reconciler (every 60 s) then activates delivery without waiting for the kubelet.

The API answer wins over the mounted files while it is known. When the API
cannot be used (no cluster, missing RBAC on an older chart, transient errors),
the mounted volume stays the fallback, so nothing regresses. Values are kept in
memory only, never logged, and never included in errors.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: A value read through the API is reused for this long before another ``get``.
DEFAULT_MAX_AGE_SECONDS = 30.0
#: A failed ``get`` is not retried before this delay (the mounted files are used meanwhile).
FAILURE_BACKOFF_SECONDS = 30.0
_WATCH_TIMEOUT_SECONDS = 300
_WATCH_RETRY_SECONDS = 10.0
_MAX_VALUE_BYTES = 1024 * 1024

STATE_UNKNOWN = "unknown"  # the API could not be read: use the mounted files
STATE_PRESENT = "present"
STATE_ABSENT = "absent"

ApiFactory = Callable[[], Awaitable[Any]]


@dataclass
class _Snapshot:
    state: str = STATE_UNKNOWN
    data: dict[str, bytes] = field(default_factory=dict, repr=False)
    read_at: float | None = None


async def _default_api_factory() -> Any:
    from greenkube.core.k8s_client import get_core_v1_api

    return await get_core_v1_api()


def _status_code(error: BaseException) -> int | None:
    status = getattr(error, "status", None)
    return status if isinstance(status, int) else None


def _decode_data(data: Any) -> dict[str, bytes]:
    if not isinstance(data, Mapping):
        return {}
    decoded: dict[str, bytes] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(raw) <= _MAX_VALUE_BYTES:
            decoded[key] = raw
    return decoded


class DefaultCredentialsSecret:
    """In-memory view of one Secret, read by name through the Kubernetes API."""

    def __init__(
        self,
        name: str,
        namespace: str | None,
        *,
        api_factory: ApiFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self.name = name
        self.namespace = namespace
        self._api_factory = api_factory or _default_api_factory
        self._monotonic = monotonic
        self._max_age = max_age_seconds
        self._snapshot = _Snapshot()
        self._failed_at: float | None = None
        self._lock = asyncio.Lock()
        self._watch_task: asyncio.Task[None] | None = None
        self.changed = asyncio.Event()

    # -- state ------------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._snapshot.state

    def value(self, key: str) -> bytes | None:
        """The key's bytes when the Secret is known present; None otherwise (absent or unknown)."""
        if self._snapshot.state != STATE_PRESENT:
            return None
        return self._snapshot.data.get(key)

    def has(self, key: str) -> bool | None:
        """True/False when the API answered, None when only the mounted files can tell."""
        if self._snapshot.state == STATE_UNKNOWN:
            return None
        value = self.value(key)
        return bool(value)

    def _apply(self, state: str, data: dict[str, bytes]) -> None:
        previous = self._snapshot
        self._snapshot = _Snapshot(state=state, data=data, read_at=self._monotonic())
        if previous.state != state or previous.data != data:
            if previous.state != state:
                logger.info("Git credentials Secret %s is %s (Kubernetes API).", self.name, state)
            self.changed.set()

    # -- reads ------------------------------------------------------------------------

    async def refresh(self) -> str:
        """``get`` the Secret now; returns the state. Never raises."""
        if not self.namespace or not self.name:
            return self._snapshot.state
        async with self._lock:
            try:
                api = await self._api_factory()
                if api is None:
                    raise RuntimeError("no Kubernetes configuration")
                secret = await api.read_namespaced_secret(name=self.name, namespace=self.namespace)
            except Exception as error:
                status = _status_code(error)
                if status == 404:
                    self._failed_at = None
                    self._apply(STATE_ABSENT, {})
                else:
                    # 403 (older chart without the Role), no cluster, transient: fall back
                    # to the mounted files rather than declaring the Secret absent.
                    if self._failed_at is None or self._snapshot.state != STATE_UNKNOWN:
                        logger.info(
                            "Git credentials Secret %s cannot be read through the API (%s); using the mounted volume.",
                            self.name,
                            status or type(error).__name__,
                        )
                    self._failed_at = self._monotonic()
                    self._snapshot = _Snapshot()
                return self._snapshot.state
            self._failed_at = None
            data = secret.get("data") if isinstance(secret, Mapping) else getattr(secret, "data", None)
            self._apply(STATE_PRESENT, _decode_data(data))
            return self._snapshot.state

    async def ensure_fresh(self) -> str:
        """Refresh when the last read is older than ``max_age`` (failures back off)."""
        now = self._monotonic()
        if self._failed_at is not None and now - self._failed_at < FAILURE_BACKOFF_SECONDS:
            return self._snapshot.state
        read_at = self._snapshot.read_at
        if read_at is not None and self._snapshot.state != STATE_UNKNOWN and now - read_at < self._max_age:
            return self._snapshot.state
        return await self.refresh()

    # -- watch ------------------------------------------------------------------------

    async def watch_once(self, *, timeout_seconds: int = _WATCH_TIMEOUT_SECONDS) -> None:
        """One watch on this Secret only (``fieldSelector=metadata.name``); never a list."""
        from kubernetes_asyncio import watch

        api = await self._api_factory()
        if api is None or not self.namespace:
            raise RuntimeError("no Kubernetes configuration")
        watcher = watch.Watch()
        try:
            async for event in watcher.stream(
                api.list_namespaced_secret,
                namespace=self.namespace,
                field_selector=f"metadata.name={self.name}",
                timeout_seconds=timeout_seconds,
            ):
                self.apply_event(event)
        finally:
            watcher.stop()

    def apply_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        obj = event.get("object")
        metadata = obj.get("metadata") if isinstance(obj, Mapping) else getattr(obj, "metadata", None)
        name = metadata.get("name") if isinstance(metadata, Mapping) else getattr(metadata, "name", None)
        if name != self.name:
            return
        if kind == "DELETED":
            self._apply(STATE_ABSENT, {})
        elif kind in ("ADDED", "MODIFIED"):
            data = obj.get("data") if isinstance(obj, Mapping) else getattr(obj, "data", None)
            self._apply(STATE_PRESENT, _decode_data(data))

    def start_watch(self) -> None:
        """Keep the Secret current in the background (controller only); idempotent."""
        if self._watch_task is not None and not self._watch_task.done():
            return
        if not self.namespace:
            return
        self._watch_task = asyncio.create_task(self._watch_forever(), name=f"watch-secret-{self.name}")

    async def _watch_forever(self) -> None:
        while True:
            try:
                await self.watch_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.debug("Git credentials Secret watch ended (%s).", type(error).__name__)
                await asyncio.sleep(_WATCH_RETRY_SECONDS)

    async def stop_watch(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


_STORES: dict[tuple[str | None, str], DefaultCredentialsSecret] = {}


def default_credentials_name() -> str:
    try:
        from greenkube.core.config import get_config

        return str(getattr(get_config(), "GITOPS_DEFAULT_CREDENTIALS_SECRET", "") or "")
    except Exception:
        return os.getenv("GITOPS_DEFAULT_CREDENTIALS_SECRET", "greenkube-git-credentials")


def default_credentials_secret(name: str | None = None) -> DefaultCredentialsSecret | None:
    """The process-wide reader of the default credentials Secret in the release namespace."""
    secret_name = name if name is not None else default_credentials_name()
    namespace = os.getenv("POD_NAMESPACE") or None
    if not secret_name or not namespace:
        return None
    key = (namespace, secret_name)
    store = _STORES.get(key)
    if store is None:
        store = _STORES[key] = DefaultCredentialsSecret(secret_name, namespace)
    return store


def reset_default_credentials_secrets() -> None:
    """Forget every process-wide reader (tests)."""
    _STORES.clear()
