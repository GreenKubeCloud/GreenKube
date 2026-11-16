-- 0162: The legacy hourly v1 ledger trigger compares every column (review of LED-04).
--
-- The PostgreSQL trigger of 0161 already compares the whole row but
-- superseded (jsonb of NEW and OLD), so nothing changes here. The function is
-- recreated unchanged so both dialects carry migration 0162.
CREATE OR REPLACE FUNCTION greenkube_v1_ledger_supersede_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NOT OLD.superseded AND NEW.superseded
        AND (to_jsonb(NEW) - 'superseded') = (to_jsonb(OLD) - 'superseded') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% rows are append-only (only superseded may be set)', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
