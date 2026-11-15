-- 0161: The v1 savings ledger becomes append-only (LED-04).
--
-- recommendation_savings_ledger and its legacy hourly table are a write-ahead
-- staging area projected into savings_ledger_entries, no longer a reporting
-- source. A row may never be changed or deleted: the only update allowed is
-- setting superseded from FALSE to TRUE (rollback review, revert, phantom
-- repair), which the ledger projector turns into a reversal entry.
CREATE OR REPLACE FUNCTION greenkube_v1_ledger_supersede_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NOT OLD.superseded AND NEW.superseded
        AND (to_jsonb(NEW) - 'superseded') = (to_jsonb(OLD) - 'superseded') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% rows are append-only (only superseded may be set)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_recommendation_savings_ledger_append_only ON recommendation_savings_ledger;
CREATE TRIGGER trg_recommendation_savings_ledger_append_only
    BEFORE UPDATE OR DELETE ON recommendation_savings_ledger
    FOR EACH ROW EXECUTE FUNCTION greenkube_v1_ledger_supersede_only();

DROP TRIGGER IF EXISTS trg_recommendation_savings_ledger_hourly_append_only ON recommendation_savings_ledger_hourly;
CREATE TRIGGER trg_recommendation_savings_ledger_hourly_append_only
    BEFORE UPDATE OR DELETE ON recommendation_savings_ledger_hourly
    FOR EACH ROW EXECUTE FUNCTION greenkube_v1_ledger_supersede_only();
