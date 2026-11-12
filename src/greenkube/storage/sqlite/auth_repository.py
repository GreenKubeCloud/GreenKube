"""SQLite storage for local accounts, sessions and API keys (roadmap F14, F15)."""

from __future__ import annotations

import json
from datetime import datetime

from greenkube.auth.models import ApiKey, AuthSession, OidcLogin, User
from greenkube.storage.base_auth_repository import AuthRepository
from greenkube.utils.time import ensure_utc

# Fixed-width UTC format so that string comparison equals time comparison.
_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

_USER_COLUMNS = (
    "id, username, password_hash, role, must_change_password, created_at, updated_at, password_changed_at, "
    "disabled_at, auth_source, external_id"
)
_OIDC_COLUMNS = "state_sha256, nonce, code_verifier, redirect_uri, return_to, created_at, expires_at"
_SESSION_COLUMNS = (
    "token_sha256, id, user_id, created_at, last_seen_at, idle_expires_at, absolute_expires_at, "
    "remember, user_agent, ip, revoked_at"
)
_KEY_COLUMNS = "id, name, prefix, secret_sha256, scopes, created_by, created_at, last_used_at, expires_at, revoked_at"


def _encode(value: datetime | None) -> str | None:
    return ensure_utc(value).strftime(_FORMAT) if value is not None else None


def _decode(value: str | None) -> datetime | None:
    return ensure_utc(value) if value else None


def _required(value: str) -> datetime:
    return ensure_utc(value)


def _user(row) -> User:
    return User(
        id=row[0],
        username=row[1],
        password_hash=row[2],
        role=row[3],
        must_change_password=bool(row[4]),
        created_at=_required(row[5]),
        updated_at=_required(row[6]),
        password_changed_at=_decode(row[7]),
        disabled_at=_decode(row[8]),
        auth_source=row[9] or "local",
        external_id=row[10],
    )


def _session(row) -> AuthSession:
    return AuthSession(
        token_sha256=row[0],
        id=row[1],
        user_id=row[2],
        created_at=_required(row[3]),
        last_seen_at=_required(row[4]),
        idle_expires_at=_required(row[5]),
        absolute_expires_at=_required(row[6]),
        remember=bool(row[7]),
        user_agent=row[8],
        ip=row[9],
        revoked_at=_decode(row[10]),
    )


def _api_key(row) -> ApiKey:
    try:
        scopes = frozenset(json.loads(row[4] or "[]"))
    except (TypeError, ValueError):
        scopes = frozenset()
    return ApiKey(
        id=row[0],
        name=row[1],
        prefix=row[2],
        secret_sha256=row[3],
        scopes=scopes,
        created_by=row[5],
        created_at=_decode(row[6]),
        last_used_at=_decode(row[7]),
        expires_at=_decode(row[8]),
        revoked_at=_decode(row[9]),
    )


def _user_values(user: User) -> tuple:
    return (
        user.id,
        user.username,
        user.password_hash,
        user.role,
        int(user.must_change_password),
        _encode(user.created_at),
        _encode(user.updated_at),
        _encode(user.password_changed_at),
        _encode(user.disabled_at),
        user.auth_source,
        user.external_id,
    )


def _oidc_login(row) -> OidcLogin:
    return OidcLogin(
        state_sha256=row[0],
        nonce=row[1],
        code_verifier=row[2],
        redirect_uri=row[3],
        return_to=row[4],
        created_at=_required(row[5]),
        expires_at=_required(row[6]),
    )


class SQLiteAuthRepository(AuthRepository):
    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    async def _fetchone(self, query: str, params: tuple = ()):
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(query, params) as cursor:
                return await cursor.fetchone()

    async def _fetchall(self, query: str, params: tuple = ()):
        async with self.db_manager.connection_scope() as conn:
            async with conn.execute(query, params) as cursor:
                return await cursor.fetchall()

    async def _write(self, query: str, params: tuple = ()) -> int:
        async with self.db_manager.transaction() as conn:
            cursor = await conn.execute(query, params)
            return cursor.rowcount

    # --- users -------------------------------------------------------------
    async def count_users(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) FROM auth_users")
        return int(row[0]) if row else 0

    async def create_user(self, user: User) -> bool:
        return (
            await self._write(
                f"INSERT INTO auth_users ({_USER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                _user_values(user),
            )
            == 1
        )

    async def create_first_user(self, user: User) -> bool:
        return (
            await self._write(
                f"INSERT INTO auth_users ({_USER_COLUMNS}) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
                "WHERE NOT EXISTS (SELECT 1 FROM auth_users) ON CONFLICT DO NOTHING",
                _user_values(user),
            )
            == 1
        )

    async def get_user(self, user_id: str) -> User | None:
        row = await self._fetchone(f"SELECT {_USER_COLUMNS} FROM auth_users WHERE id = ?", (user_id,))
        return _user(row) if row else None

    async def get_user_by_username(self, username: str) -> User | None:
        row = await self._fetchone(f"SELECT {_USER_COLUMNS} FROM auth_users WHERE username = ?", (username,))
        return _user(row) if row else None

    async def set_password(
        self, user_id: str, password_hash: str, *, must_change_password: bool, now: datetime
    ) -> bool:
        stamp = _encode(now)
        return (
            await self._write(
                "UPDATE auth_users SET password_hash = ?, must_change_password = ?, password_changed_at = ?, "
                "updated_at = ? WHERE id = ?",
                (password_hash, int(must_change_password), stamp, stamp, user_id),
            )
            == 1
        )

    async def any_user_must_change_password(self) -> bool:
        row = await self._fetchone("SELECT 1 FROM auth_users WHERE must_change_password = 1 LIMIT 1")
        return row is not None

    # --- SSO identities and roles (F16) -----------------------------------------
    async def get_user_by_external_id(self, external_id: str) -> User | None:
        row = await self._fetchone(f"SELECT {_USER_COLUMNS} FROM auth_users WHERE external_id = ?", (external_id,))
        return _user(row) if row else None

    async def list_users(self) -> list[User]:
        rows = await self._fetchall(f"SELECT {_USER_COLUMNS} FROM auth_users ORDER BY username")
        return [_user(row) for row in rows]

    async def set_role(self, user_id: str, role: str, *, now: datetime) -> bool:
        return (
            await self._write(
                "UPDATE auth_users SET role = ?, updated_at = ? WHERE id = ?", (role, _encode(now), user_id)
            )
            == 1
        )

    async def set_disabled(self, user_id: str, disabled_at: datetime | None, *, now: datetime) -> bool:
        return (
            await self._write(
                "UPDATE auth_users SET disabled_at = ?, updated_at = ? WHERE id = ?",
                (_encode(disabled_at), _encode(now), user_id),
            )
            == 1
        )

    async def create_oidc_login(self, login: OidcLogin) -> None:
        await self._write(
            f"INSERT INTO auth_oidc_logins ({_OIDC_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                login.state_sha256,
                login.nonce,
                login.code_verifier,
                login.redirect_uri,
                login.return_to,
                _encode(login.created_at),
                _encode(login.expires_at),
            ),
        )

    async def consume_oidc_login(self, state_sha256: str) -> OidcLogin | None:
        async with self.db_manager.transaction() as conn:
            async with conn.execute(
                f"DELETE FROM auth_oidc_logins WHERE state_sha256 = ? RETURNING {_OIDC_COLUMNS}", (state_sha256,)
            ) as cursor:
                row = await cursor.fetchone()
        return _oidc_login(row) if row else None

    async def purge_oidc_logins(self, *, now: datetime) -> int:
        return await self._write("DELETE FROM auth_oidc_logins WHERE expires_at <= ?", (_encode(now),))

    # --- sessions ----------------------------------------------------------
    async def create_session(self, session: AuthSession) -> None:
        await self._write(
            f"INSERT INTO auth_sessions ({_SESSION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.token_sha256,
                session.id,
                session.user_id,
                _encode(session.created_at),
                _encode(session.last_seen_at),
                _encode(session.idle_expires_at),
                _encode(session.absolute_expires_at),
                int(session.remember),
                session.user_agent,
                session.ip,
                _encode(session.revoked_at),
            ),
        )

    async def get_session(self, token_sha256: str) -> AuthSession | None:
        row = await self._fetchone(
            f"SELECT {_SESSION_COLUMNS} FROM auth_sessions WHERE token_sha256 = ?",
            (token_sha256,),
        )
        return _session(row) if row else None

    async def touch_session(self, token_sha256: str, *, last_seen_at: datetime, idle_expires_at: datetime) -> None:
        await self._write(
            "UPDATE auth_sessions SET last_seen_at = ?, idle_expires_at = ? "
            "WHERE token_sha256 = ? AND revoked_at IS NULL",
            (_encode(last_seen_at), _encode(idle_expires_at), token_sha256),
        )

    async def revoke_session(self, token_sha256: str, *, now: datetime) -> bool:
        return (
            await self._write(
                "UPDATE auth_sessions SET revoked_at = ? WHERE token_sha256 = ? AND revoked_at IS NULL",
                (_encode(now), token_sha256),
            )
            == 1
        )

    async def revoke_session_by_id(self, session_id: str, *, user_id: str, now: datetime) -> bool:
        return (
            await self._write(
                "UPDATE auth_sessions SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
                (_encode(now), session_id, user_id),
            )
            == 1
        )

    async def revoke_user_sessions(self, user_id: str, *, now: datetime) -> int:
        return await self._write(
            "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (_encode(now), user_id),
        )

    async def list_user_sessions(self, user_id: str, *, now: datetime) -> list[AuthSession]:
        stamp = _encode(now)
        rows = await self._fetchall(
            f"SELECT {_SESSION_COLUMNS} FROM auth_sessions WHERE user_id = ? AND revoked_at IS NULL "
            "AND idle_expires_at > ? AND absolute_expires_at > ? ORDER BY last_seen_at DESC, id",
            (user_id, stamp, stamp),
        )
        return [_session(row) for row in rows]

    async def purge_sessions(self, *, now: datetime, revoked_before: datetime) -> int:
        stamp = _encode(now)
        return await self._write(
            "DELETE FROM auth_sessions WHERE absolute_expires_at <= ? OR idle_expires_at <= ? "
            "OR (revoked_at IS NOT NULL AND revoked_at <= ?)",
            (stamp, stamp, _encode(revoked_before)),
        )

    # --- API keys ----------------------------------------------------------
    async def create_api_key(self, key: ApiKey) -> None:
        await self._write(
            f"INSERT INTO api_keys ({_KEY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key.id,
                key.name,
                key.prefix,
                key.secret_sha256,
                json.dumps(sorted(key.scopes)),
                key.created_by,
                _encode(key.created_at),
                _encode(key.last_used_at),
                _encode(key.expires_at),
                _encode(key.revoked_at),
            ),
        )

    async def get_api_key(self, key_id: str) -> ApiKey | None:
        row = await self._fetchone(f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE id = ?", (key_id,))
        return _api_key(row) if row else None

    async def list_api_keys(self) -> list[ApiKey]:
        rows = await self._fetchall(f"SELECT {_KEY_COLUMNS} FROM api_keys ORDER BY created_at DESC, id")
        return [_api_key(row) for row in rows]

    async def revoke_api_key(self, key_id: str, *, now: datetime) -> bool:
        return (
            await self._write(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_encode(now), key_id),
            )
            == 1
        )

    async def touch_api_key(self, key_id: str, *, now: datetime) -> None:
        await self._write("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_encode(now), key_id))
