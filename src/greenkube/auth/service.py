"""Local accounts, server-side sessions and scoped API keys (roadmap F14, F15).

The service owns every security decision that does not depend on HTTP:
password verification, session issuance and expiry, password changes and API
key resolution. The HTTP layer (``greenkube.api.dependencies`` and the auth
router) adds cookies, CSRF and rate limits.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from greenkube.auth.models import ApiKey, AuthSession, User
from greenkube.auth.passwords import (
    dummy_verify,
    hash_password,
    needs_rehash,
    validate_new_password,
    verify_password,
)
from greenkube.auth.principal import ROLE_SCOPES, Principal, Role, normalize_scopes
from greenkube.auth.roles import NO_ROLE, role_for_groups, role_rank
from greenkube.auth.settings import AuthSettings
from greenkube.auth.tokens import (
    api_key_prefix,
    constant_time_equals,
    csrf_token_for,
    new_api_key,
    new_public_id,
    new_session_token,
    parse_api_key,
    sha256_hex,
)
from greenkube.storage.base_auth_repository import AuthRepository

logger = logging.getLogger(__name__)

# Activity is written back at most this often per session or API key, so a busy
# dashboard does not turn every read into a database write.
TOUCH_INTERVAL = timedelta(seconds=60)
# Revoked sessions are kept this long for the sessions list, then purged.
REVOKED_RETENTION = timedelta(days=1)
MAX_USERNAME_LENGTH = 128
MAX_USER_AGENT_LENGTH = 512
MAX_API_KEY_NAME_LENGTH = 100


class AuthError(Exception):
    """An authentication or account operation was refused.

    ``code`` is stable for clients; ``message`` is safe to show (it never says
    whether a username exists).
    """

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


INVALID_CREDENTIALS = "Invalid username or password."
#: Stored as the password hash of SSO accounts: never a valid scrypt hash, so no password signs them in.
SSO_PASSWORD_HASH = "!sso"
SSO_SOURCES = frozenset({"proxy", "oidc"})


@dataclass(frozen=True)
class IssuedSession:
    token: str
    session: AuthSession
    user: User

    @property
    def csrf_token(self) -> str:
        return csrf_token_for(self.token)


@dataclass(frozen=True)
class CreatedApiKey:
    key: ApiKey
    plaintext: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def user_principal(user: User, session: AuthSession | None = None, token: str | None = None) -> Principal:
    role = user.role if user.role in ROLE_SCOPES else Role.VIEWER.value
    return Principal(
        kind="user",
        name=user.username,
        role=role,
        scopes=ROLE_SCOPES[role],
        session_id=session.id if session else None,
        csrf_token=csrf_token_for(token) if token else None,
        must_change_password=user.must_change_password,
        user_id=user.id,
        auth_source=user.auth_source,
    )


def api_key_principal(key: ApiKey) -> Principal:
    return Principal(kind="apikey", name=key.name, scopes=frozenset(key.scopes), api_key_id=key.id)


class AuthService:
    def __init__(
        self,
        repository: AuthRepository,
        *,
        settings: Callable[[], AuthSettings] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.repository = repository
        self._settings = settings or _settings_from_config
        self._clock = clock
        self._users_exist = False

    @property
    def settings(self) -> AuthSettings:
        return self._settings()

    def now(self) -> datetime:
        return self._clock()

    # --- users -------------------------------------------------------------
    async def users_exist(self) -> bool:
        """Whether at least one account exists (cached once true: accounts are never deleted)."""
        if not self._users_exist:
            self._users_exist = await self.repository.count_users() > 0
        return self._users_exist

    async def create_user(
        self, username: str, password: str, *, role: str = Role.VIEWER.value, must_change_password: bool = False
    ) -> User:
        user = self._new_user(username, await _hash(password), role=role, must_change_password=must_change_password)
        if not await self.repository.create_user(user):
            raise AuthError("USERNAME_TAKEN", "This username is already taken.", status_code=409)
        self._users_exist = True
        return user

    async def create_first_admin(self, password: str, *, must_change_password: bool) -> User | None:
        """Create the bootstrap admin unless an account already exists."""
        user = self._new_user(
            self.settings.admin_username,
            await _hash(password),
            role=Role.ADMIN.value,
            must_change_password=must_change_password,
        )
        created = await self.repository.create_first_user(user)
        self._users_exist = True if created else await self.repository.count_users() > 0
        return user if created else None

    def _new_user(self, username: str, password_hash: str, *, role: str, must_change_password: bool) -> User:
        username = normalize_username(username)
        if not username or len(username) > MAX_USERNAME_LENGTH:
            raise AuthError("INVALID_USERNAME", "The username is invalid.", status_code=422)
        if role not in ROLE_SCOPES:
            raise AuthError("INVALID_ROLE", "The role is invalid.", status_code=422)
        now = self.now()
        return User(
            id=uuid.uuid4().hex,
            username=username,
            password_hash=password_hash,
            role=role,
            must_change_password=must_change_password,
            created_at=now,
            updated_at=now,
            password_changed_at=now,
        )

    # --- sign-in -----------------------------------------------------------
    async def login(
        self,
        username: str,
        password: str,
        *,
        remember: bool = False,
        ip: str | None = None,
        user_agent: str | None = None,
        previous_token: str | None = None,
    ) -> IssuedSession:
        """Verify credentials and issue a brand-new session token.

        A token presented before sign-in is revoked, never reused, so an attacker
        cannot plant a session identifier (session fixation).
        """
        normalized = normalize_username(username)
        if not normalized or len(normalized) > MAX_USERNAME_LENGTH or not password:
            raise AuthError("INVALID_CREDENTIALS", INVALID_CREDENTIALS, status_code=401)
        user = await self.repository.get_user_by_username(normalized)
        if user is None or user.disabled_at is not None or not user.is_local:
            # SSO accounts have no password: they cost the same time as an unknown user.
            await _run_kdf(dummy_verify, password)
            raise AuthError("INVALID_CREDENTIALS", INVALID_CREDENTIALS, status_code=401)
        if not await _run_kdf(verify_password, password, user.password_hash):
            raise AuthError("INVALID_CREDENTIALS", INVALID_CREDENTIALS, status_code=401)
        if needs_rehash(user.password_hash):
            await self.repository.set_password(
                user.id, await _hash(password), must_change_password=user.must_change_password, now=self.now()
            )
        if previous_token:
            await self.repository.revoke_session(sha256_hex(previous_token), now=self.now())
        return await self._issue(user, remember=remember, ip=ip, user_agent=user_agent)

    async def issue_session(
        self,
        user: User,
        *,
        remember: bool = False,
        ip: str | None = None,
        user_agent: str | None = None,
        previous_token: str | None = None,
    ) -> IssuedSession:
        """Issue a new session for an already authenticated user (SSO, F16).

        Like :meth:`login`, a token presented beforehand is revoked, never reused.
        """
        if user.disabled_at is not None:
            raise AuthError("ACCOUNT_DISABLED", "This account is disabled.", status_code=403)
        if previous_token:
            await self.repository.revoke_session(sha256_hex(previous_token), now=self.now())
        return await self._issue(user, remember=remember, ip=ip, user_agent=user_agent)

    async def _issue(self, user: User, *, remember: bool, ip: str | None, user_agent: str | None) -> IssuedSession:
        settings = self.settings
        now = self.now()
        lifetime = settings.remember_lifetime if remember else settings.absolute_lifetime
        absolute = now + lifetime
        token = new_session_token()
        session = AuthSession(
            token_sha256=sha256_hex(token),
            id=new_public_id(),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            idle_expires_at=min(now + settings.idle_timeout, absolute),
            absolute_expires_at=absolute,
            remember=remember,
            user_agent=(user_agent or "")[:MAX_USER_AGENT_LENGTH] or None,
            ip=ip,
        )
        await self.repository.create_session(session)
        self._users_exist = True
        return IssuedSession(token=token, session=session, user=user)

    async def resolve_session(self, token: str) -> tuple[AuthSession, User] | None:
        """Return the active session and its user for a cookie token, sliding the idle expiry."""
        if not token or len(token) > 256:
            return None
        digest = sha256_hex(token)
        session = await self.repository.get_session(digest)
        now = self.now()
        if session is None or not session.is_active(now):
            return None
        user = await self.repository.get_user(session.user_id)
        if user is None or user.disabled_at is not None:
            return None
        if now - session.last_seen_at >= TOUCH_INTERVAL:
            idle_expires_at = min(now + self.settings.idle_timeout, session.absolute_expires_at)
            await self.repository.touch_session(digest, last_seen_at=now, idle_expires_at=idle_expires_at)
        return session, user

    async def logout(self, token: str) -> bool:
        if not token:
            return False
        return await self.repository.revoke_session(sha256_hex(token), now=self.now())

    async def change_password(
        self,
        user: User,
        current_password: str,
        new_password: str,
        *,
        remember: bool = False,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """Change a password, revoke every session of the user and issue a new one."""
        if not user.is_local:
            raise AuthError(
                "PASSWORD_MANAGED_BY_SSO",
                "This account signs in through single sign-on; change the password at your identity provider.",
                status_code=409,
            )
        if not await _run_kdf(verify_password, current_password or "", user.password_hash):
            raise AuthError("INVALID_CREDENTIALS", "The current password is incorrect.", status_code=403)
        reason = validate_new_password(new_password or "", username=user.username)
        if reason:
            raise AuthError("WEAK_PASSWORD", reason, status_code=422)
        if new_password == current_password:
            raise AuthError("WEAK_PASSWORD", "Choose a password different from the current one.", status_code=422)
        now = self.now()
        await self.repository.set_password(user.id, await _hash(new_password), must_change_password=False, now=now)
        await self.repository.revoke_user_sessions(user.id, now=now)
        refreshed = await self.repository.get_user(user.id) or user
        return await self._issue(refreshed, remember=remember, ip=ip, user_agent=user_agent)

    async def list_sessions(self, user_id: str) -> list[AuthSession]:
        return await self.repository.list_user_sessions(user_id, now=self.now())

    async def revoke_session(self, session_id: str, *, user_id: str) -> bool:
        return await self.repository.revoke_session_by_id(session_id, user_id=user_id, now=self.now())

    async def purge_expired_sessions(self) -> int:
        now = self.now()
        purged = await self.repository.purge_sessions(now=now, revoked_before=now - REVOKED_RETENTION)
        await self.repository.purge_oidc_logins(now=now)
        return purged

    # --- single sign-on and roles (F16) --------------------------------------
    def sso_role(self, groups: frozenset[str] | set[str]) -> str | None:
        """The role the configured group mapping grants, ``None`` for no access."""
        settings = self.settings
        return role_for_groups(groups, settings.role_groups, settings.default_role)

    async def sso_user(self, source: str, subject: str, username: str, groups: frozenset[str] | set[str]) -> User:
        """Find or create the account of an SSO identity and apply the group mapping.

        The role is recomputed from the groups at every sign-in, so removing a
        user from an IdP group takes effect at their next session. An SSO identity
        never takes over a local account with the same username.
        """
        if source not in SSO_SOURCES or not subject:
            raise AuthError("INVALID_IDENTITY", "The identity provider sent no usable identity.", status_code=401)
        role = self.sso_role(groups)
        if role is None or role == NO_ROLE:
            raise AuthError(
                "NO_ROLE",
                "Your account is not in any group that grants access to GreenKube. Ask an administrator.",
                status_code=403,
            )
        external_id = f"{source}:{subject}"
        user = await self.repository.get_user_by_external_id(external_id)
        if user is None:
            user = await self._create_sso_user(source, external_id, username, role)
        if user.disabled_at is not None:
            raise AuthError("ACCOUNT_DISABLED", "This account is disabled.", status_code=403)
        if user.role != role:
            await self.repository.set_role(user.id, role, now=self.now())
            logger.info("Role of %s set to %s from its groups.", user.username, role)
            user = await self.repository.get_user(user.id) or user
        return user

    async def _create_sso_user(self, source: str, external_id: str, username: str, role: str) -> User:
        user = self._new_user(username, SSO_PASSWORD_HASH, role=role, must_change_password=False)
        user = replace(user, auth_source=source, external_id=external_id)
        if await self.repository.create_user(user):
            self._users_exist = True
            logger.info("Created %s account %s with role %s.", source, user.username, role)
            return user
        # A concurrent sign-in created it, or the username belongs to another account.
        existing = await self.repository.get_user_by_external_id(external_id)
        if existing is not None:
            return existing
        raise AuthError(
            "ACCOUNT_CONFLICT",
            "Another account already uses this username; an SSO identity never takes over an existing account.",
            status_code=409,
        )

    async def list_users(self) -> list[User]:
        return await self.repository.list_users()

    async def _ensure_admin_remains(self, user: User) -> None:
        """Refuse to demote or disable the last active administrator (lockout)."""
        if user.role != Role.ADMIN.value or user.disabled_at is not None:
            return
        admins = [
            other
            for other in await self.repository.list_users()
            if other.role == Role.ADMIN.value and other.disabled_at is None and other.id != user.id
        ]
        if not admins:
            raise AuthError("LAST_ADMIN", "At least one active administrator must remain.", status_code=409)

    async def set_user_role(self, user_id: str, role: str) -> User:
        if role_rank(role) < 0:
            raise AuthError("INVALID_ROLE", "The role must be viewer, operator or admin.", status_code=422)
        user = await self.repository.get_user(user_id)
        if user is None:
            raise AuthError("USER_NOT_FOUND", "User not found.", status_code=404)
        if role != user.role and role != Role.ADMIN.value:
            await self._ensure_admin_remains(user)
        await self.repository.set_role(user.id, role, now=self.now())
        return await self.repository.get_user(user.id) or user

    async def set_user_disabled(self, user_id: str, disabled: bool) -> User:
        """Disable (revoking every session) or re-enable an account."""
        user = await self.repository.get_user(user_id)
        if user is None:
            raise AuthError("USER_NOT_FOUND", "User not found.", status_code=404)
        now = self.now()
        if disabled:
            await self._ensure_admin_remains(user)
            await self.repository.set_disabled(user.id, user.disabled_at or now, now=now)
            await self.repository.revoke_user_sessions(user.id, now=now)
        else:
            await self.repository.set_disabled(user.id, None, now=now)
        return await self.repository.get_user(user.id) or user

    async def initial_password_pending(self) -> bool:
        return await self.repository.any_user_must_change_password()

    # --- API keys ----------------------------------------------------------
    async def create_api_key(
        self,
        name: str,
        scopes: list[str] | tuple[str, ...] | set[str] | frozenset[str],
        *,
        created_by: str,
        expires_in: timedelta | None = None,
    ) -> CreatedApiKey:
        name = (name or "").strip()
        if not name or len(name) > MAX_API_KEY_NAME_LENGTH:
            raise AuthError(
                "INVALID_NAME", f"The name must have 1 to {MAX_API_KEY_NAME_LENGTH} characters.", status_code=422
            )
        try:
            normalized = normalize_scopes(scopes)
        except ValueError as error:
            raise AuthError("INVALID_SCOPES", str(error), status_code=422) from None
        if expires_in is not None and expires_in <= timedelta(0):
            raise AuthError("INVALID_EXPIRY", "The expiry must be in the future.", status_code=422)
        key_id, secret, plaintext = new_api_key()
        now = self.now()
        key = ApiKey(
            id=key_id,
            name=name,
            prefix=api_key_prefix(key_id),
            secret_sha256=sha256_hex(secret),
            scopes=normalized,
            created_by=created_by,
            created_at=now,
            expires_at=now + expires_in if expires_in else None,
        )
        await self.repository.create_api_key(key)
        return CreatedApiKey(key=key, plaintext=plaintext)

    async def list_api_keys(self) -> list[ApiKey]:
        return await self.repository.list_api_keys()

    async def revoke_api_key(self, key_id: str) -> bool:
        return await self.repository.revoke_api_key(key_id, now=self.now())

    async def resolve_api_key(self, token: str) -> ApiKey | None:
        parsed = parse_api_key(token)
        if parsed is None:
            return None
        key_id, secret = parsed
        key = await self.repository.get_api_key(key_id)
        if key is None or not constant_time_equals(sha256_hex(secret), key.secret_sha256):
            return None
        now = self.now()
        if not key.is_active(now):
            return None
        if key.last_used_at is None or now - key.last_used_at >= TOUCH_INTERVAL:
            await self.repository.touch_api_key(key.id, now=now)
        return key


# Each scrypt run allocates about 32 MiB. Without a bound, a burst of concurrent
# sign-ins (all passing the throttle check before any failure is recorded) would
# run one hash per executor thread and could exhaust a 128-512 MiB pod. Extra
# requests queue on this per-event-loop semaphore instead.
MAX_CONCURRENT_KDF = 2
_kdf_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _kdf_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _kdf_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_KDF)
        _kdf_semaphores[loop] = semaphore
    return semaphore


async def _run_kdf[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run a password hash or verification off the event loop, at most ``MAX_CONCURRENT_KDF`` at once."""
    async with _kdf_semaphore():
        return await asyncio.to_thread(func, *args, **kwargs)


async def _hash(password: str) -> str:
    return await _run_kdf(hash_password, password)


def _settings_from_config() -> AuthSettings:
    from greenkube.core.config import get_config

    return AuthSettings.from_config(get_config())


def build_auth_service(db_manager) -> AuthService:
    """Create the service for the configured database backend."""
    db_type = getattr(db_manager, "db_type", None)
    if db_type is None:
        from greenkube.core.config import get_config

        db_type = get_config().DB_TYPE
    if db_type == "postgres":
        from greenkube.storage.postgres.auth_repository import PostgresAuthRepository

        return AuthService(PostgresAuthRepository(db_manager))
    from greenkube.storage.sqlite.auth_repository import SQLiteAuthRepository

    return AuthService(SQLiteAuthRepository(db_manager))
