-- 0054: local accounts and server-side sessions (roadmap F14).
-- Passwords are stored as scrypt hashes; a session row is keyed by the SHA-256
-- of its opaque cookie token, so a database read never yields a usable cookie.
-- Timestamps are fixed-width UTC strings, so string order is time order.
-- Additive and idempotent.

CREATE TABLE IF NOT EXISTS auth_users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    password_changed_at TEXT,
    disabled_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_sha256 TEXT PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    remember INTEGER NOT NULL DEFAULT 0,
    user_agent TEXT,
    ip TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_absolute_expires_at ON auth_sessions (absolute_expires_at);
