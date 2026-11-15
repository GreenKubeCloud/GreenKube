-- 0135: Baseline health signals of a change measurement (MEA-07).
--
-- The health gates of a change decision are compared with the same signals
-- over the baseline window (restarts per replica-hour, CPU throttling ratio,
-- readiness ratio, OOM kills), frozen with the snapshot. Snapshots frozen
-- before this column keep an empty object and their gates are judged on
-- absolute levels, labelled baseline_missing.
ALTER TABLE baseline_snapshots ADD COLUMN health TEXT NOT NULL DEFAULT '{}';
