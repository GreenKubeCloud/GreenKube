-- 0293: Provenance of every savings accrual (LED-11).
--
-- v1 rows now store the annual rates they were accrued from, the currency of
-- the cost and the measurement whose rate a measured row uses, read at
-- accrual time. The v2 entries projected from them carry annual_rate (part of
-- the hash payload of ledger/4 entries), so value = annual_rate x seconds /
-- 31557600 can be checked from the entry alone. Rows and entries stored
-- before keep NULL: no row is updated and no stored hash changes.
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS annual_cost_rate DOUBLE PRECISION;
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS annual_co2e_rate DOUBLE PRECISION;
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS currency TEXT;
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS measurement_id TEXT;
ALTER TABLE savings_ledger_entries ADD COLUMN IF NOT EXISTS annual_rate DOUBLE PRECISION;
