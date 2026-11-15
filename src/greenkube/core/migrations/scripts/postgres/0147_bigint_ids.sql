-- 0147: 64-bit ids and sequences (STO-05).
--
-- Every INSERT ... ON CONFLICT DO UPDATE consumes a sequence value, also when
-- it ends as an update, so a 32-bit SERIAL id of a metric table reaches
-- 2147483647 on a large or long-lived install and every write then fails.
-- Every id column of type integer of this schema becomes BIGINT, and its
-- sequence too.
--
-- The sequence change is instant. The column change rewrites the table under
-- an ACCESS EXCLUSIVE lock, so a table of at most 256 MB (indexes included)
-- is altered here. combined_metrics and combined_metrics_hourly above that
-- size get a BIGINT shadow column id__i8 kept equal to id by a trigger and a
-- row in online_column_upgrades: the controller backfills it in batches,
-- builds its unique index concurrently and swaps it in with one short
-- transaction (see greenkube.core.migrations.online). The session setting
-- greenkube.online_upgrade_min_bytes overrides the 256 MB limit.
DO $migration$
DECLARE
    tbl TEXT;
    seq TEXT;
    online_limit_bytes BIGINT := COALESCE(NULLIF(current_setting('greenkube.online_upgrade_min_bytes', true), '')::BIGINT, 268435456);
BEGIN
    FOR tbl IN
        SELECT c.relname::TEXT
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relkind = 'r'
          AND a.attname = 'id'
          AND NOT a.attisdropped
          AND a.atttypid = 'integer'::regtype
        ORDER BY c.relname
    LOOP
        seq := pg_get_serial_sequence(quote_ident(tbl), 'id');
        IF seq IS NOT NULL THEN
            EXECUTE format('ALTER SEQUENCE %s AS BIGINT', seq);
        END IF;
        IF tbl IN ('combined_metrics', 'combined_metrics_hourly')
           AND pg_total_relation_size(to_regclass(quote_ident(tbl))) > online_limit_bytes THEN
            EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS id__i8 BIGINT', tbl);
            EXECUTE format(
                'CREATE OR REPLACE FUNCTION %I() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN NEW.id__i8 := NEW.id; RETURN NEW; END $body$',
                tbl || '_online_upgrade_id'
            );
            EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', tbl || '_online_upgrade_id', tbl);
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION %I()',
                tbl || '_online_upgrade_id', tbl, tbl || '_online_upgrade_id'
            );
            INSERT INTO online_column_upgrades (table_name, column_name, target_type)
            VALUES (tbl, 'id', 'bigint')
            ON CONFLICT (table_name, column_name) DO NOTHING;
        ELSE
            EXECUTE format('ALTER TABLE %I ALTER COLUMN id TYPE BIGINT', tbl);
        END IF;
    END LOOP;
END
$migration$;
