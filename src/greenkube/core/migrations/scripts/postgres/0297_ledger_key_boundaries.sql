-- 0297: Key boundaries of the savings ledger chain (LED-05).
--
-- The HMAC-signed status row names the key that signed it (key_id) and, as
-- JSON, the seq at which each key took over the chain (key_boundaries): the
-- first key at first_keyed_seq, then one per greenkube db ledger-rotate-key.
-- A previous key then verifies only the entries below its successor's
-- boundary. Rows stored before keep NULL.
ALTER TABLE ledger_chain_status ADD COLUMN IF NOT EXISTS key_id TEXT;
ALTER TABLE ledger_chain_status ADD COLUMN IF NOT EXISTS key_boundaries TEXT;
