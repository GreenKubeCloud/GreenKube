-- 0291: Kind of a v1 savings ledger row that is not an accrual (LED-12).
--
-- The projector recognised drift reversals by their signs (both quantities
-- negative), so a reversal of a saving with mixed signs was projected as two
-- accruals. The drift monitor now writes row_kind = 'drift_reversal'. Rows
-- stored before keep NULL and are recognised by the sign rule as before: no
-- row is updated and no ledger v2 entry changes.
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS row_kind TEXT;
