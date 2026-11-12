"""``greenkube auth``: account recovery from inside the cluster (roadmap F14, F16, N11).

Once the initial admin password has been used, GreenKube erases it from the
``<release>-initial-admin`` Secret. A forgotten password is reset from the API
Pod, which already holds the database credentials::

    kubectl -n greenkube exec deploy/greenkube-api -- greenkube auth reset-password admin

The command prints a new random password once, forces a change at the next
sign-in, re-enables the account and signs out every session of it. It never
touches other accounts.
"""

from __future__ import annotations

import asyncio

import typer
from typing_extensions import Annotated

app = typer.Typer(name="auth", help="Account recovery (reset a local account's password).")


async def reset_password(username: str) -> str:
    """Reset a local account; return the new password. Raises ``ValueError`` when impossible."""
    from greenkube.auth.passwords import generate_password, hash_password
    from greenkube.auth.service import build_auth_service, normalize_username
    from greenkube.core.config import get_config
    from greenkube.core.db import DatabaseManager

    manager = DatabaseManager(get_config())
    await manager.connect()
    try:
        service = build_auth_service(manager)
        user = await service.repository.get_user_by_username(normalize_username(username))
        if user is None:
            raise ValueError(f"No account named {username!r}.")
        if not user.is_local:
            raise ValueError(
                f"{user.username!r} signs in through single sign-on ({user.auth_source}): it has no password."
            )
        password = generate_password()
        now = service.now()
        encoded = await asyncio.to_thread(hash_password, password)
        await service.repository.set_password(user.id, encoded, must_change_password=True, now=now)
        await service.repository.set_disabled(user.id, None, now=now)
        await service.repository.revoke_user_sessions(user.id, now=now)
        return password
    finally:
        await manager.close()


@app.command("reset-password")
def reset_password_command(
    username: Annotated[str, typer.Argument(help="Local account to reset.")] = "admin",
) -> None:
    """Give a local account a new random password (changed at its next sign-in) and sign out its sessions."""
    try:
        password = asyncio.run(reset_password(username))
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"New password for {username.strip().lower()} (change it at the next sign-in):", err=True)
    typer.echo(password)
