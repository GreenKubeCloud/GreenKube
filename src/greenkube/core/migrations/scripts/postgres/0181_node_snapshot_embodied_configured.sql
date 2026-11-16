-- 0181: Node snapshots carry only the operator embodied value (NRG-07).
-- The embodied service used to write the Boavizta or fallback value into node snapshots while the
-- node-analysis loop wrote NULL, opening a new version on every cycle. Those values were derived,
-- not configured: they are cleared (Boavizta values live in instance_carbon_profiles), then
-- consecutive versions of a node that are equal on every compared column are collapsed into
-- the first one, which takes the last valid_to, is_current and the latest last_seen.

UPDATE node_snapshots_scd SET embodied_emissions_kg = NULL WHERE embodied_emissions_kg IS NOT NULL;
UPDATE node_snapshots SET embodied_emissions_kg = NULL WHERE embodied_emissions_kg IS NOT NULL;

CREATE TEMP TABLE node_scd_marked ON COMMIT DROP AS
SELECT
    id,
    node_name,
    valid_from,
    valid_to,
    is_current,
    last_seen,
    CASE
        WHEN LAG(id) OVER w IS NULL THEN 1
        WHEN instance_type IS NOT DISTINCT FROM LAG(instance_type) OVER w
            AND cpu_capacity_cores IS NOT DISTINCT FROM LAG(cpu_capacity_cores) OVER w
            AND architecture IS NOT DISTINCT FROM LAG(architecture) OVER w
            AND cloud_provider IS NOT DISTINCT FROM LAG(cloud_provider) OVER w
            AND region IS NOT DISTINCT FROM LAG(region) OVER w
            AND zone IS NOT DISTINCT FROM LAG(zone) OVER w
            AND node_pool IS NOT DISTINCT FROM LAG(node_pool) OVER w
            AND memory_capacity_bytes IS NOT DISTINCT FROM LAG(memory_capacity_bytes) OVER w
            AND is_active IS NOT DISTINCT FROM LAG(is_active) OVER w
            THEN 0
        ELSE 1
    END AS starts
FROM node_snapshots_scd
WINDOW w AS (PARTITION BY node_name ORDER BY valid_from, id);

CREATE TEMP TABLE node_scd_islands ON COMMIT DROP AS
SELECT
    id,
    valid_from,
    valid_to,
    is_current,
    last_seen,
    FIRST_VALUE(id) OVER (PARTITION BY node_name, island ORDER BY valid_from, id) AS keeper_id
FROM (
    SELECT m.*, SUM(starts) OVER (PARTITION BY node_name ORDER BY valid_from, id) AS island
    FROM node_scd_marked m
) numbered;

CREATE TEMP TABLE node_scd_spans ON COMMIT DROP AS
SELECT
    keeper_id,
    (ARRAY_AGG(valid_to ORDER BY valid_from DESC, id DESC))[1] AS last_valid_to,
    BOOL_OR(is_current) AS any_current,
    MAX(last_seen) AS max_last_seen,
    COUNT(*) AS versions
FROM node_scd_islands
GROUP BY keeper_id
HAVING COUNT(*) > 1;

UPDATE node_snapshots_scd s
SET valid_to = sp.last_valid_to,
    is_current = sp.any_current,
    last_seen = GREATEST(s.last_seen, sp.max_last_seen)
FROM node_scd_spans sp
WHERE s.id = sp.keeper_id;

DELETE FROM node_snapshots_scd s
USING node_scd_islands i
WHERE s.id = i.id AND i.id <> i.keeper_id;
