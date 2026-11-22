-- 0290: Keyed hash chain of the savings ledger v2 (LED-05).
--
-- With LEDGER_CHAIN_KEY (a Kubernetes Secret, never stored in the database)
-- new entries are chained with HMAC-SHA256 and name the key by a public
-- fingerprint in chain_key_id. Entries stored before keep NULL: they were
-- chained with plain sha256 over a payload without this column, so their
-- hash stays valid. No existing row is updated.
ALTER TABLE savings_ledger_entries ADD COLUMN chain_key_id TEXT;
