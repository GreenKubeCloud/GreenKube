-- 0053: Input high-water marks of the workload usage rollup (E3).
-- The rollup records the highest raw row id it has read per input table. The
-- next run re-rolls every hour that received rows above that mark, so late
-- collection (catch-up after an outage, `greenkube start --last`) is never
-- skipped, however old the hour is within the lookback.
CREATE TABLE IF NOT EXISTS workload_usage_rollup_marks (
    input_table TEXT PRIMARY KEY,
    last_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);
