"""Storage contract for local accounts, sessions and API keys (roadmap F14, F15)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from greenkube.auth.models import ApiKey, AuthSession, User


class AuthRepository(ABC):
    """Persist users, server-side sessions and scoped API keys.

    Sessions live in the shared database so that every API replica sees them.
    """

    # --- users -------------------------------------------------------------
    @abstractmethod
    async def count_users(self) -> int: ...

    @abstractmethod
    async def create_user(self, user: User) -> bool:
        """Insert ``user``; ``False`` when the username is taken."""

    @abstractmethod
    async def create_first_user(self, user: User) -> bool:
        """Insert ``user`` only while no user exists; ``False`` otherwise."""

    @abstractmethod
    async def get_user(self, user_id: str) -> User | None: ...

    @abstractmethod
    async def get_user_by_username(self, username: str) -> User | None: ...

    @abstractmethod
    async def set_password(
        self, user_id: str, password_hash: str, *, must_change_password: bool, now: datetime
    ) -> bool: ...

    @abstractmethod
    async def any_user_must_change_password(self) -> bool: ...

    # --- sessions ----------------------------------------------------------
    @abstractmethod
    async def create_session(self, session: AuthSession) -> None: ...

    @abstractmethod
    async def get_session(self, token_sha256: str) -> AuthSession | None: ...

    @abstractmethod
    async def touch_session(self, token_sha256: str, *, last_seen_at: datetime, idle_expires_at: datetime) -> None: ...

    @abstractmethod
    async def revoke_session(self, token_sha256: str, *, now: datetime) -> bool: ...

    @abstractmethod
    async def revoke_session_by_id(self, session_id: str, *, user_id: str, now: datetime) -> bool: ...

    @abstractmethod
    async def revoke_user_sessions(self, user_id: str, *, now: datetime) -> int: ...

    @abstractmethod
    async def list_user_sessions(self, user_id: str, *, now: datetime) -> list[AuthSession]:
        """Active (unrevoked, unexpired) sessions of a user, newest first."""

    @abstractmethod
    async def purge_sessions(self, *, now: datetime, revoked_before: datetime) -> int:
        """Delete expired sessions and sessions revoked before ``revoked_before``."""

    # --- API keys ----------------------------------------------------------
    @abstractmethod
    async def create_api_key(self, key: ApiKey) -> None: ...

    @abstractmethod
    async def get_api_key(self, key_id: str) -> ApiKey | None: ...

    @abstractmethod
    async def list_api_keys(self) -> list[ApiKey]: ...

    @abstractmethod
    async def revoke_api_key(self, key_id: str, *, now: datetime) -> bool: ...

    @abstractmethod
    async def touch_api_key(self, key_id: str, *, now: datetime) -> None: ...
