"""PostgreSQL storage for local accounts, sessions and API keys (roadmap F14, F15).

Sessions are shared by every API replica through this table.
"""

from __future__ import annotations

from datetime import datetime

from greenkube.auth.models import ApiKey, AuthSession, User
from greenkube.storage.base_auth_repository import AuthRepository
from greenkube.utils.time import ensure_utc

_USER_COLUMNS = (
    "id, username, password_hash, role, must_change_password, created_at, updated_at, password_changed_at, disabled_at"
)
_SESSION_COLUMNS = (
    "token_sha256, id, user_id, created_at, last_seen_at, idle_expires_at, absolute_expires_at, "
    "remember, user_agent, ip, revoked_at"
)
_KEY_COLUMNS = "id, name, prefix, secret_sha256, scopes, created_by, created_at, last_used_at, expires_at, revoked_at"


def _utc(value: datetime | None) -> datetime | None:
    return ensure_utc(value) if value is not None else None


def _user(row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        must_change_password=bool(row["must_change_password"]),
        created_at=ensure_utc(row["created_at"]),
        updated_at=ensure_utc(row["updated_at"]),
        password_changed_at=_utc(row["password_changed_at"]),
        disabled_at=_utc(row["disabled_at"]),
    )


def _session(row) -> AuthSession:
    return AuthSession(
        token_sha256=row["token_sha256"],
        id=row["id"],
        user_id=row["user_id"],
        created_at=ensure_utc(row["created_at"]),
        last_seen_at=ensure_utc(row["last_seen_at"]),
        idle_expires_at=ensure_utc(row["idle_expires_at"]),
        absolute_expires_at=ensure_utc(row["absolute_expires_at"]),
        remember=bool(row["remember"]),
        user_agent=row["user_agent"],
        ip=row["ip"],
        revoked_at=_utc(row["revoked_at"]),
    )


def _api_key(row) -> ApiKey:
    return ApiKey(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        secret_sha256=row["secret_sha256"],
        scopes=frozenset(row["scopes"] or ()),
        created_by=row["created_by"],
        created_at=_utc(row["created_at"]),
        last_used_at=_utc(row["last_used_at"]),
        expires_at=_utc(row["expires_at"]),
        revoked_at=_utc(row["revoked_at"]),
    )


def _user_values(user: User) -> tuple:
    return (
        user.id,
        user.username,
        user.password_hash,
        user.role,
        user.must_change_password,
        ensure_utc(user.created_at),
        ensure_utc(user.updated_at),
        _utc(user.password_changed_at),
        _utc(user.disabled_at),
    )


def _affected(status: str) -> int:
    """Row count from an asyncpg command tag such as ``UPDATE 1`` or ``INSERT 0 1``."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (AttributeError, ValueError):
        return 0


class PostgresAuthRepository(AuthRepository):
    def __init__(self, db_manager) -> None:
        self.db_manager = db_manager

    # --- users -------------------------------------------------------------
    async def count_users(self) -> int:
        async with self.db_manager.connection_scope() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM auth_users"))

    async def create_user(self, user: User) -> bool:
        async with self.db_manager.connection_scope() as conn:
            status = await conn.execute(
                f"INSERT INTO auth_users ({_USER_COLUMNS}) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                "ON CONFLICT (username) DO NOTHING",
                *_user_values(user),
            )
        return _affected(status) == 1

    async def create_first_user(self, user: User) -> bool:
        # Every API replica may race to bootstrap the first admin: the advisory
        # lock serializes them, and the losers see an existing user.
        async with self.db_manager.transaction() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('greenkube.auth.first_user'))")
            status = await conn.execute(
                f"INSERT INTO auth_users ({_USER_COLUMNS}) SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9 "
                "WHERE NOT EXISTS (SELECT 1 FROM auth_users) ON CONFLICT (username) DO NOTHING",
                *_user_values(user),
            )
        return _affected(status) == 1

    async def get_user(self, user_id: str) -> User | None:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(f"SELECT {_USER_COLUMNS} FROM auth_users WHERE id = $1", user_id)
        return _user(row) if row else None

    async def get_user_by_username(self, username: str) -> User | None:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(f"SELECT {_USER_COLUMNS} FROM auth_users WHERE username = $1", username)
        return _user(row) if row else None

    async def set_password(
        self, user_id: str, password_hash: str, *, must_change_password: bool, now: datetime
    ) -> bool:
        async with self.db_manager.connection_scope() as conn:
            status = await conn.execute(
                "UPDATE auth_users SET password_hash = $1, must_change_password = $2, password_changed_at = $3, "
                "updated_at = $3 WHERE id = $4",
                password_hash,
                must_change_password,
                ensure_utc(now),
                user_id,
            )
        return _affected(status) == 1

    async def any_user_must_change_password(self) -> bool:
        async with self.db_manager.connection_scope() as conn:
            return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM auth_users WHERE must_change_password)"))

    # --- sessions ----------------------------------------------------------
    async def create_session(self, session: AuthSession) -> None:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                f"INSERT INTO auth_sessions ({_SESSION_COLUMNS}) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                session.token_sha256,
                session.id,
                session.user_id,
                ensure_utc(session.created_at),
                ensure_utc(session.last_seen_at),
                ensure_utc(session.idle_expires_at),
                ensure_utc(session.absolute_expires_at),
                session.remember,
                session.user_agent,
                session.ip,
                _utc(session.revoked_at),
            )

    async def get_session(self, token_sha256: str) -> AuthSession | None:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(
                f"SELECT {_SESSION_COLUMNS} FROM auth_sessions WHERE token_sha256 = $1",
                token_sha256,
            )
        return _session(row) if row else None

    async def touch_session(self, token_sha256: str, *, last_seen_at: datetime, idle_expires_at: datetime) -> None:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                "UPDATE auth_sessions SET last_seen_at = $1, idle_expires_at = $2 "
                "WHERE token_sha256 = $3 AND revoked_at IS NULL",
                ensure_utc(last_seen_at),
                ensure_utc(idle_expires_at),
                token_sha256,
            )

    async def revoke_session(self, token_sha256: str, *, now: datetime) -> bool:
        async with self.db_manager.connection_scope() as conn:
            status = await conn.execute(
                "UPDATE auth_sessions SET revoked_at = $1 WHERE token_sha256 = $2 AND revoked_at IS NULL",
                ensure_utc(now),
                token_sha256,
            )
        return _affected(status) == 1

    async def revoke_session_by_id(self, session_id: str, *, user_id: str, now: datetime) -> bool:
        async with self.db_manager.connection_scope() as conn:
            status = await conn.execute(
                "UPDATE auth_sessions SET revoked_at = $1 WHERE id = $2 AND user_id = $3 AND revoked_at IS NULL",
                ensure_utc(now),
                session_id,
                user_id,
            )
        return _affected(status) == 1

    async def revoke_user_sessions(self, user_id: str, *, now: datetime) -> int:
        async with self.db_manager.connection_scope() as conn:
            status = await conn.execute(
                "UPDATE auth_sessions SET revoked_at = $1 WHERE user_id = $2 AND revoked_at IS NULL",
                ensure_utc(now),
                user_id,
            )
        return _affected(status)

    async def list_user_sessions(self, user_id: str, *, now: datetime) -> list[AuthSession]:
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(
                f"SELECT {_SESSION_COLUMNS} FROM auth_sessions WHERE user_id = $1 AND revoked_at IS NULL "
                "AND idle_expires_at > $2 AND absolute_expires_at > $2 ORDER BY last_seen_at DESC, id",
                user_id,
                ensure_utc(now),
            )
        return [_session(row) for row in rows]

    async def purge_sessions(self, *, now: datetime, revoked_before: datetime) -> int:
        async with self.db_manager.connection_scope() as conn:
            status = await conn.execute(
                "DELETE FROM auth_sessions WHERE absolute_expires_at <= $1 OR idle_expires_at <= $1 "
                "OR (revoked_at IS NOT NULL AND revoked_at <= $2)",
                ensure_utc(now),
                ensure_utc(revoked_before),
            )
        return _affected(status)

    # --- API keys ----------------------------------------------------------
    async def create_api_key(self, key: ApiKey) -> None:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute(
                f"INSERT INTO api_keys ({_KEY_COLUMNS}) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                key.id,
                key.name,
                key.prefix,
                key.secret_sha256,
                sorted(key.scopes),
                key.created_by,
                _utc(key.created_at),
                _utc(key.last_used_at),
                _utc(key.expires_at),
                _utc(key.revoked_at),
            )

    async def get_api_key(self, key_id: str) -> ApiKey | None:
        async with self.db_manager.connection_scope() as conn:
            row = await conn.fetchrow(f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE id = $1", key_id)
        return _api_key(row) if row else None

    async def list_api_keys(self) -> list[ApiKey]:
        async with self.db_manager.connection_scope() as conn:
            rows = await conn.fetch(f"SELECT {_KEY_COLUMNS} FROM api_keys ORDER BY created_at DESC, id")
        return [_api_key(row) for row in rows]

    async def revoke_api_key(self, key_id: str, *, now: datetime) -> bool:
        async with self.db_manager.connection_scope() as conn:
            status = await conn.execute(
                "UPDATE api_keys SET revoked_at = $1 WHERE id = $2 AND revoked_at IS NULL",
                ensure_utc(now),
                key_id,
            )
        return _affected(status) == 1

    async def touch_api_key(self, key_id: str, *, now: datetime) -> None:
        async with self.db_manager.connection_scope() as conn:
            await conn.execute("UPDATE api_keys SET last_used_at = $1 WHERE id = $2", ensure_utc(now), key_id)
