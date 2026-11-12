-- 0093: single sign-on identities and roles (roadmap F16). Additive.
--
-- auth_source tells how an account signs in: "local" (password), "proxy"
-- (trusted authentication proxy headers) or "oidc" (native OpenID Connect).
-- external_id is the stable identity at the provider ("proxy:<user>" or
-- "oidc:<subject>"), NULL for local accounts. An SSO identity is never merged
-- into a local account: the unique index keeps one account per identity.
ALTER TABLE auth_users ADD COLUMN auth_source TEXT NOT NULL DEFAULT 'local';
ALTER TABLE auth_users ADD COLUMN external_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_external_id ON auth_users (external_id);
