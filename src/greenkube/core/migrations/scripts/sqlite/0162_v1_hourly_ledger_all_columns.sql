-- 0162: The legacy hourly v1 ledger trigger compares every column (review of LED-04).
--
-- 0161 compared only six columns of recommendation_savings_ledger_hourly, so
-- an update setting superseded could change the other ones in the same
-- statement. The trigger is recreated to compare every column but superseded.
DROP TRIGGER IF EXISTS trg_recommendation_savings_ledger_hourly_no_update;
CREATE TRIGGER IF NOT EXISTS trg_recommendation_savings_ledger_hourly_no_update
BEFORE UPDATE ON recommendation_savings_ledger_hourly
WHEN NOT (
    OLD.superseded = 0 AND NEW.superseded <> 0
    AND NEW.id IS OLD.id
    AND NEW.recommendation_id IS OLD.recommendation_id
    AND NEW.cluster_name IS OLD.cluster_name
    AND NEW.namespace IS OLD.namespace
    AND NEW.recommendation_type IS OLD.recommendation_type
    AND NEW.co2e_saved_grams IS OLD.co2e_saved_grams
    AND NEW.cost_saved_dollars IS OLD.cost_saved_dollars
    AND NEW.sample_count IS OLD.sample_count
    AND NEW.hour_bucket IS OLD.hour_bucket
    AND NEW.measurement_method IS OLD.measurement_method
    AND NEW.baseline_value IS OLD.baseline_value
    AND NEW.actual_value IS OLD.actual_value
    AND NEW.confidence IS OLD.confidence
    AND NEW.period_start IS OLD.period_start
    AND NEW.period_end IS OLD.period_end
    AND NEW.legacy_compressed IS OLD.legacy_compressed
)
BEGIN
    SELECT RAISE(ABORT, 'recommendation_savings_ledger_hourly rows are append-only (only superseded may be set)');
END;
