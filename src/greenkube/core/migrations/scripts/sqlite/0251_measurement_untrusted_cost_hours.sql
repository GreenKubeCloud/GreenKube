-- 0251: Untrusted cost hours of each decided change measurement (MEA-05).
--
-- untrusted_cost_hours and baseline_untrusted_cost_hours hold the observed
-- workload hours without trusted cost in the post-window and in the baseline
-- window, so an inconclusive cost verdict says why. Measurements decided
-- before this migration keep NULL (their evidence digest leaves them out).
ALTER TABLE change_measurements ADD COLUMN untrusted_cost_hours REAL;
ALTER TABLE change_measurements ADD COLUMN baseline_untrusted_cost_hours REAL;
