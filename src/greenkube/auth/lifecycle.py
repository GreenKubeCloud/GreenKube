"""Lifecycle of authentication: store and first admin (API), session purge (controller).

The API starts no background loop (M8): the daily session purge is a controller
loop (``session-purge``) that calls :func:`purge_sessions_once`.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from greenkube.auth.bootstrap import ensure_initial_admin, initial_admin_secret_from_config
from greenkube.auth.service import AuthService, build_auth_service

logger = logging.getLogger(__name__)

PURGE_INTERVAL_SECONDS = 24 * 3600
BOOTSTRAP_TIMEOUT_SECONDS = 60
BOOTSTRAP_RETRY_SECONDS = 30
#: Backoff while the controller migrates the schema (no limit: a 0.3.x upgrade can take long).
SCHEMA_WAIT_INITIAL_SECONDS = 2.0
SCHEMA_WAIT_MAX_SECONDS = 30.0


async def wait_for_schema(schema_ready: Callable[[], Awaitable[bool]]) -> None:
    """Wait, without a deadline, until the controller has migrated the schema (F11).

    The API never migrates; the controller does at startup and may take minutes
    on a large upgrade, or start much later (``controller.replicas=0`` during the
    SQLite copy). The first admin can only be created once the tables exist.
    """
    delay = SCHEMA_WAIT_INITIAL_SECONDS
    logged = False
    while True:
        try:
            if await schema_ready():
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # database not reachable yet: keep waiting
            logger.debug("Schema check failed (%s).", type(error).__name__)
        if not logged:
            logger.info("Initial admin bootstrap waits for the controller to migrate the database schema.")
            logged = True
        await asyncio.sleep(delay)
        delay = min(delay * 2, SCHEMA_WAIT_MAX_SECONDS)


async def bootstrap_initial_admin(
    service: AuthService,
    *,
    attempts: int = 10,
    schema_ready: Callable[[], Awaitable[bool]] | None = None,
) -> str:
    """Create the first admin once the schema is current, retrying while the API server or database is not ready."""
    from greenkube.core.config import get_config

    if schema_ready is not None:
        # The retry budget only starts once the schema exists.
        await wait_for_schema(schema_ready)
    for attempt in range(1, attempts + 1):
        try:
            cfg = get_config()
            return await asyncio.wait_for(
                ensure_initial_admin(service, cfg, initial_admin_secret_from_config(cfg)),
                timeout=BOOTSTRAP_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Initial admin bootstrap attempt %d/%d failed (%s).", attempt, attempts, type(error).__name__
            )
            if attempt < attempts:
                await asyncio.sleep(BOOTSTRAP_RETRY_SECONDS)
    return "failed"


async def purge_sessions_once(service: AuthService) -> int:
    """Delete expired and long-revoked sessions (idempotent)."""
    purged = await service.purge_expired_sessions()
    if purged:
        logger.info("Purged %d expired session(s).", purged)
    return purged


async def run_session_purge_loop(service: AuthService, interval: float = PURGE_INTERVAL_SECONDS) -> None:
    """Standalone purge loop, kept for embedders; the controller schedules ``purge_sessions_once``."""
    await asyncio.sleep(random.uniform(60, 600))
    while True:
        try:
            await purge_sessions_once(service)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Session purge failed (%s).", type(error).__name__)
        await asyncio.sleep(interval)


def start_auth_runtime(app, db_manager) -> list[asyncio.Task]:
    """Attach the account service to ``app.state`` and bootstrap the first admin.

    The bootstrap is a startup task that ends once the admin exists; the API runs no
    periodic mutation (the session purge runs in the controller, M8).
    """
    try:
        service = build_auth_service(db_manager)
    except Exception as error:
        # Fail closed: without the store only Bearer keys authenticate.
        logger.error("Account store unavailable (%s); browser sign-in is disabled.", type(error).__name__)
        app.state.auth_service = None
        return []
    app.state.auth_service = service

    async def schema_ready() -> bool:
        return (await db_manager.schema_status()).current

    return [
        asyncio.create_task(bootstrap_initial_admin(service, schema_ready=schema_ready), name="auth-initial-admin"),
    ]
