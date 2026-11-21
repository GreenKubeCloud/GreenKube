-- 0294: Signed savings ledger chain status (LED-05).
--
-- With LEDGER_CHAIN_KEY the verified head stored in ledger_chain_status is
-- signed with an HMAC of (cluster, head seq, head hash, keyed flag), so an
-- edited or forged row fails verification, and a missing row fails once the
-- chain exists. Rows stored before keep a NULL signature: they are accepted
-- (and signed) only while the chain holds no keyed entry.
ALTER TABLE ledger_chain_status ADD COLUMN head_keyed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ledger_chain_status ADD COLUMN signature TEXT;
