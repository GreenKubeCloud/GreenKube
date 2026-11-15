-- 0145: Additive measures of the metric tables in double precision (NRG-06).
--
-- joules, co2e_grams, embodied_co2e_grams, total_cost, pue and grid_intensity
-- were REAL (float4) on the raw, container and hourly metric tables. SUM over
-- float4 accumulates in float4 and drifts by percents beyond about one million
-- rows. They become DOUBLE PRECISION. Stored values keep their 7 significant
-- digits.
--
-- REAL to DOUBLE PRECISION is not binary coercible: ALTER COLUMN TYPE rewrites
-- the table under an ACCESS EXCLUSIVE lock held until the migration batch
-- commits. A table of at most 256 MB (indexes included) is altered here, which
-- takes seconds. A larger table only gets a DOUBLE PRECISION shadow column per
-- measure (column name plus the suffix __f8, metadata only) kept equal to the
-- measure by a BEFORE INSERT OR UPDATE trigger, and a row in
-- online_column_upgrades. The controller then backfills the shadow columns in
-- short batches by id and swaps them in with one short transaction (drop the
-- REAL column, rename the shadow), see greenkube.core.migrations.online.
-- The session setting greenkube.online_upgrade_min_bytes overrides the 256 MB
-- limit (tests use 0 to force the online path).
CREATE TABLE IF NOT EXISTS online_column_upgrades (
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    target_type TEXT NOT NULL,
    backfilled_through BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (table_name, column_name)
);

DO $migration$
DECLARE
    tbl TEXT;
    real_columns TEXT[];
    clause TEXT;
    copies TEXT;
    online_limit_bytes BIGINT := COALESCE(NULLIF(current_setting('greenkube.online_upgrade_min_bytes', true), '')::BIGINT, 268435456);
BEGIN
    FOREACH tbl IN ARRAY ARRAY['combined_metrics', 'combined_metrics_hourly', 'container_metrics', 'container_metrics_hourly'] LOOP
        IF to_regclass(tbl) IS NULL THEN
            CONTINUE;
        END IF;
        SELECT array_agg(a.attname::TEXT ORDER BY a.attnum) INTO real_columns
        FROM pg_attribute a
        WHERE a.attrelid = to_regclass(tbl)
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND a.atttypid = 'real'::regtype
          AND a.attname IN ('joules', 'co2e_grams', 'embodied_co2e_grams', 'total_cost', 'pue', 'grid_intensity');
        IF real_columns IS NULL THEN
            CONTINUE;
        END IF;
        IF pg_total_relation_size(to_regclass(tbl)) <= online_limit_bytes THEN
            SELECT string_agg(format('ALTER COLUMN %I TYPE DOUBLE PRECISION', c), ', ') INTO clause
            FROM unnest(real_columns) AS c;
            EXECUTE format('ALTER TABLE %I %s', tbl, clause);
        ELSE
            SELECT string_agg(format('ADD COLUMN IF NOT EXISTS %I DOUBLE PRECISION', c || '__f8'), ', '),
                   string_agg(format('NEW.%I := NEW.%I', c || '__f8', c), '; ')
              INTO clause, copies
            FROM unnest(real_columns) AS c;
            EXECUTE format('ALTER TABLE %I %s', tbl, clause);
            EXECUTE format(
                'CREATE OR REPLACE FUNCTION %I() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN %s; RETURN NEW; END $body$',
                tbl || '_online_upgrade', copies
            );
            EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', tbl || '_online_upgrade', tbl);
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION %I()',
                tbl || '_online_upgrade', tbl, tbl || '_online_upgrade'
            );
            INSERT INTO online_column_upgrades (table_name, column_name, target_type)
            SELECT tbl, c, 'double precision' FROM unnest(real_columns) AS c
            ON CONFLICT (table_name, column_name) DO NOTHING;
        END IF;
    END LOOP;
END
$migration$;
