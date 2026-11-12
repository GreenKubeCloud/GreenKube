-- 0094: pending OpenID Connect sign-ins (roadmap F16). Additive.
--
-- One row per authorization request, keyed by the SHA-256 of its state value.
-- It keeps the nonce and the PKCE code verifier server-side, so that every API
-- replica can finish the sign-in. A row is deleted when the callback consumes
-- it (single use) or once it expires (ten minutes).
CREATE TABLE IF NOT EXISTS auth_oidc_logins (
    state_sha256 TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    return_to TEXT NOT NULL DEFAULT '/',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_oidc_logins_expires_at ON auth_oidc_logins (expires_at);
