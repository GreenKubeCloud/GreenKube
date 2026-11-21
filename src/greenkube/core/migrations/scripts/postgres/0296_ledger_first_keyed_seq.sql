-- 0296: Signed boundary between unkeyed and keyed savings ledger entries (LED-05).
--
-- The first keyed append records the seq of the first keyed entry in the
-- HMAC-signed status row. Unkeyed entries below it were written before the
-- key and are accepted whatever their period (an earlier wrong clock could
-- write future periods). Rows stored before keep NULL.
ALTER TABLE ledger_chain_status ADD COLUMN IF NOT EXISTS first_keyed_seq BIGINT;
