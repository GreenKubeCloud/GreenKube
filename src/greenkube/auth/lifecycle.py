"""Lifecycle of authentication: store and first admin (API), session purge (controller).

The API starts no background loop (M8): the daily session purge is a controller
loop (``session-purge``) that calls :func:`purge_sessions_once`.
"""

from __future__ import annotations

import asyncio
import logging
import random

from greenkube.auth.bootstrap import ensure_initial_admin, initial_admin_secret_from_config
from greenkube.auth.service import AuthService, build_auth_service

logger = logging.getLogger(__name__)

PURGE_INTERVAL_SECONDS = 24 * 3600
BOOTSTRAP_TIMEOUT_SECONDS = 60
BOOTSTRAP_RETRY_SECONDS = 30


async def bootstrap_initial_admin(service: AuthService, *, attempts: int = 10) -> str:
    """Create the first admin, retrying while the API server or database is not ready."""
    from greenkube.core.config import get_config

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
    return [
        asyncio.create_task(bootstrap_initial_admin(service), name="auth-initial-admin"),
    ]
