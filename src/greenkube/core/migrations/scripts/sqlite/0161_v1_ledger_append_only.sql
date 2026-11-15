-- 0161: The v1 savings ledger becomes append-only (LED-04).
--
-- recommendation_savings_ledger and its legacy hourly table are a write-ahead
-- staging area projected into savings_ledger_entries, no longer a reporting
-- source. A row may never be changed or deleted: the only update allowed is
-- setting superseded from 0 to 1 (rollback review, revert, phantom repair),
-- which the ledger projector turns into a reversal entry.
CREATE TRIGGER IF NOT EXISTS trg_recommendation_savings_ledger_no_update
BEFORE UPDATE ON recommendation_savings_ledger
WHEN NOT (
    OLD.superseded = 0 AND NEW.superseded <> 0
    AND NEW.id IS OLD.id
    AND NEW.recommendation_id IS OLD.recommendation_id
    AND NEW.cluster_name IS OLD.cluster_name
    AND NEW.namespace IS OLD.namespace
    AND NEW.recommendation_type IS OLD.recommendation_type
    AND NEW.co2e_saved_grams IS OLD.co2e_saved_grams
    AND NEW.cost_saved_dollars IS OLD.cost_saved_dollars
    AND NEW.period_seconds IS OLD.period_seconds
    AND NEW.timestamp IS OLD.timestamp
    AND NEW.period_start IS OLD.period_start
    AND NEW.period_end IS OLD.period_end
    AND NEW.slot_start IS OLD.slot_start
    AND NEW.measurement_method IS OLD.measurement_method
    AND NEW.baseline_value IS OLD.baseline_value
    AND NEW.actual_value IS OLD.actual_value
    AND NEW.confidence IS OLD.confidence
    AND NEW.basis IS OLD.basis
)
BEGIN
    SELECT RAISE(ABORT, 'recommendation_savings_ledger rows are append-only (only superseded may be set)');
END;
CREATE TRIGGER IF NOT EXISTS trg_recommendation_savings_ledger_no_delete
BEFORE DELETE ON recommendation_savings_ledger
BEGIN
    SELECT RAISE(ABORT, 'recommendation_savings_ledger rows are append-only (only superseded may be set)');
END;
CREATE TRIGGER IF NOT EXISTS trg_recommendation_savings_ledger_hourly_no_update
BEFORE UPDATE ON recommendation_savings_ledger_hourly
WHEN NOT (
    OLD.superseded = 0 AND NEW.superseded <> 0
    AND NEW.id IS OLD.id
    AND NEW.recommendation_id IS OLD.recommendation_id
    AND NEW.cluster_name IS OLD.cluster_name
    AND NEW.co2e_saved_grams IS OLD.co2e_saved_grams
    AND NEW.cost_saved_dollars IS OLD.cost_saved_dollars
    AND NEW.hour_bucket IS OLD.hour_bucket
)
BEGIN
    SELECT RAISE(ABORT, 'recommendation_savings_ledger_hourly rows are append-only (only superseded may be set)');
END;
CREATE TRIGGER IF NOT EXISTS trg_recommendation_savings_ledger_hourly_no_delete
BEFORE DELETE ON recommendation_savings_ledger_hourly
BEGIN
    SELECT RAISE(ABORT, 'recommendation_savings_ledger_hourly rows are append-only (only superseded may be set)');
END;
