-- 0181: Node snapshots carry only the operator embodied value (NRG-07).
-- The embodied service used to write the Boavizta or fallback value into node snapshots while the
-- node-analysis loop wrote NULL, opening a new version on every cycle. Those values were derived,
-- not configured: they are cleared (Boavizta values live in instance_carbon_profiles), then
-- consecutive versions of a node that are equal on every compared column are collapsed into
-- the first one, which takes the last valid_to, is_current and the latest last_seen.

UPDATE node_snapshots_scd SET embodied_emissions_kg = NULL WHERE embodied_emissions_kg IS NOT NULL;
UPDATE node_snapshots SET embodied_emissions_kg = NULL WHERE embodied_emissions_kg IS NOT NULL;

DROP TABLE IF EXISTS temp.node_scd_islands;

CREATE TEMP TABLE node_scd_islands AS
SELECT
    id,
    valid_from,
    valid_to,
    is_current,
    last_seen,
    FIRST_VALUE(id) OVER (PARTITION BY node_name, island ORDER BY valid_from, id) AS keeper_id
FROM (
    SELECT
        marked.*,
        SUM(starts) OVER (PARTITION BY node_name ORDER BY valid_from, id) AS island
    FROM (
        SELECT
            id,
            node_name,
            valid_from,
            valid_to,
            is_current,
            last_seen,
            CASE
                WHEN LAG(id) OVER w IS NULL THEN 1
                WHEN instance_type IS LAG(instance_type) OVER w
                    AND cpu_capacity_cores IS LAG(cpu_capacity_cores) OVER w
                    AND architecture IS LAG(architecture) OVER w
                    AND cloud_provider IS LAG(cloud_provider) OVER w
                    AND region IS LAG(region) OVER w
                    AND zone IS LAG(zone) OVER w
                    AND node_pool IS LAG(node_pool) OVER w
                    AND memory_capacity_bytes IS LAG(memory_capacity_bytes) OVER w
                    AND is_active IS LAG(is_active) OVER w
                    THEN 0
                ELSE 1
            END AS starts
        FROM node_snapshots_scd
        WINDOW w AS (PARTITION BY node_name ORDER BY valid_from, id)
    ) marked
) numbered;

UPDATE node_snapshots_scd
SET valid_to = (
        SELECT i.valid_to FROM temp.node_scd_islands i
        WHERE i.keeper_id = node_snapshots_scd.id
        ORDER BY i.valid_from DESC, i.id DESC LIMIT 1
    ),
    is_current = (
        SELECT MAX(i.is_current) FROM temp.node_scd_islands i WHERE i.keeper_id = node_snapshots_scd.id
    ),
    last_seen = (
        SELECT MAX(i.last_seen) FROM temp.node_scd_islands i WHERE i.keeper_id = node_snapshots_scd.id
    )
WHERE id IN (
    SELECT keeper_id FROM temp.node_scd_islands GROUP BY keeper_id HAVING COUNT(*) > 1
);

DELETE FROM node_snapshots_scd
WHERE id IN (SELECT id FROM temp.node_scd_islands WHERE id <> keeper_id);

DROP TABLE IF EXISTS temp.node_scd_islands;
