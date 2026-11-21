-- 0298: Key boundaries in the audit of ledger head acceptances and rotations (LED-05).
--
-- ledger-rotate-key and ledger-accept-head record the key boundaries they
-- signed into the status row (JSON), so a rebuilt set stays auditable.
ALTER TABLE ledger_head_acceptances ADD COLUMN key_boundaries TEXT;
