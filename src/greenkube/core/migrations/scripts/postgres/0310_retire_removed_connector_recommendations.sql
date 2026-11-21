-- 0310: Retire the open recommendations of the removed connectors (roadmap B.3, B.0).
--
-- The Kubecost, GKE recommender, Azure Advisor and Polaris connectors are removed.
-- An active recommendation whose chosen source is one of them is retired: it
-- becomes "stale" with a "stale" event of reason "type_removed", never deleted.
-- Recommendations already in a pull request, measured or in the ledger are not
-- touched, and the ledger is never rewritten. Their signals, runs and inbox rows
-- stay (no table is dropped); nothing writes them any more.

INSERT INTO recommendation_events (recommendation_id, event_type, actor, payload, created_at)
SELECT id, 'stale', 'system',
       '{"reason": "type_removed", "source": "' || source || '", "migration": "0310"}',
       NOW()
FROM recommendation_history
WHERE status = 'active'
  AND source IN ('kubecost', 'polaris', 'gke', 'azure_advisor');

UPDATE recommendation_history
SET status = 'stale', updated_at = NOW()
WHERE status = 'active'
  AND source IN ('kubecost', 'polaris', 'gke', 'azure_advisor');
