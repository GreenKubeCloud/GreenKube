-- 0122: per-step collection record (COL-04).
-- One row per collected, empty or failed step, and one row per abandoned range (status gap),
-- so a period with holes is detectably incomplete. Steps without a row are unknown.
-- pods_without_cpu, opencost_total_cost and attributed_cost reconcile the rows of the step (COL-03).

CREATE TABLE IF NOT EXISTS collection_windows (
    cluster_uid TEXT NOT NULL,
    source TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    expected_steps INTEGER NOT NULL,
    steps_with_data INTEGER NOT NULL DEFAULT 0,
    pod_rows INTEGER NOT NULL DEFAULT 0,
    cpu_series INTEGER NOT NULL DEFAULT 0,
    pods_without_cpu INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    opencost_total_cost DOUBLE PRECISION,
    attributed_cost DOUBLE PRECISION,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_uid, source, window_start)
);

CREATE INDEX IF NOT EXISTS idx_collection_windows_status
    ON collection_windows (cluster_uid, source, status, window_start);
