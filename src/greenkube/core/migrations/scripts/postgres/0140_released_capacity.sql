-- 0140: Per-container requests of workload revisions and measured released capacity (WP-08.1).
--
-- The applied detector records the requests and limits of every container
-- (JSON: container -> cpu_request_millicores, memory_request_bytes,
-- cpu_limit_millicores, memory_limit_bytes) with each workload spec
-- observation, so a change's released capacity can be measured from the specs
-- alone. Each decided change measurement stores that released capacity and
-- its capacity verdict (JSON). Rows stored before this migration keep NULL.
ALTER TABLE workload_spec_observations ADD COLUMN IF NOT EXISTS container_resources JSONB;
ALTER TABLE change_measurements ADD COLUMN IF NOT EXISTS capacity JSONB;
