-- 0141: The last resolved realization context, kept across restarts (IMP-12).
--
-- The realization factors per node pool were a process-local cache: after a
-- controller restart, scans projected with the unobserved factor 1.0 until
-- the first autoscaler read. One row (id 1) holds the last resolved context
-- (pools, node to pool mapping, default pool and notes as JSON); a scan loads
-- it when the process has not observed the cluster yet and it is younger
-- than 24 hours.
CREATE TABLE IF NOT EXISTS realization_context (
    id INTEGER PRIMARY KEY,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    payload JSONB NOT NULL
);
