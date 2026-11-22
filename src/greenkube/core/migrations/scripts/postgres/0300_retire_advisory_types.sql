-- 0300: retire the recommendation types of the removed native advisory analyzers (roadmap B.1).
--
-- Zombie pods, idle namespaces, off-peak scaling, autoscaling candidates,
-- carbon-aware scheduling, over/under-provisioned nodes and the Karpenter
-- NodePool heuristic are gone in 0.4: nothing produces these types any more.
-- Their open rows (active or ignored) become stale with a stale event of
-- reason type_removed, never deleted. Applied, verified and other finished
-- rows keep their status, and the savings ledger is not touched.
INSERT INTO recommendation_events (recommendation_id, event_type, actor, payload, created_at)
SELECT id, 'stale', 'system', '{"reason": "type_removed", "from_status": "' || status || '"}', NOW()
FROM recommendation_history
WHERE status IN ('active', 'ignored')
  AND type IN (
      'ZOMBIE_POD', 'IDLE_NAMESPACE', 'OFF_PEAK_SCALING', 'AUTOSCALING_CANDIDATE',
      'CARBON_AWARE_SCHEDULING', 'OVERPROVISIONED_NODE', 'UNDERUTILIZED_NODE'
  );

UPDATE recommendation_history
SET status = 'stale', updated_at = NOW()
WHERE status IN ('active', 'ignored')
  AND type IN (
      'ZOMBIE_POD', 'IDLE_NAMESPACE', 'OFF_PEAK_SCALING', 'AUTOSCALING_CANDIDATE',
      'CARBON_AWARE_SCHEDULING', 'OVERPROVISIONED_NODE', 'UNDERUTILIZED_NODE'
  );
