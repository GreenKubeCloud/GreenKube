-- 0160: Accounting basis of every savings ledger amount (LED-01).
--
-- Projected savings were accrued on the expected-billable basis (allocated
-- cost x realization factor) and measured savings on the allocated basis, so
-- the two methods were not comparable. From ledger/2 projections accrue on the
-- allocated basis like measurements, and every amount names its basis:
-- allocated, billable_expected, billed or declared.
--
-- v1 rows stored before this migration are backfilled from their method
-- (projected rows keep billable_expected, they are never rewritten). v2
-- entries are append-only: entries stored before keep NULL, so their hash
-- stays valid, and readers infer the same basis from the method.
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS basis TEXT;
ALTER TABLE savings_ledger_entries ADD COLUMN IF NOT EXISTS basis TEXT;
UPDATE recommendation_savings_ledger SET basis = CASE
    WHEN measurement_method = 'measured' THEN 'allocated'
    WHEN measurement_method = 'user_declared' THEN 'declared'
    ELSE 'billable_expected'
END
WHERE basis IS NULL;
