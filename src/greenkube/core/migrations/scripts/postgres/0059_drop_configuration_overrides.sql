-- 0059: Drop the unused runtime configuration overrides table (roadmap M4).
-- Migration 0020 created it for runtime configuration persistence that was
-- never wired in: no process wrote or read it. Service endpoints and tokens
-- come from Helm values and Secrets.
DROP TABLE IF EXISTS configuration_overrides;
