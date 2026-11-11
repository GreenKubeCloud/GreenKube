-- 0063: Evidence-based deletion recommendations (user-reported bug: an
-- "orphaned" LoadBalancer was the cluster's Cilium Gateway).
--
-- deletion_candidates remembers since when a LoadBalancer Service or a
-- PersistentVolume has continuously looked unused, so a deletion is only
-- recommended once the condition has persisted (DELETION_MIN_UNUSED_DAYS).
CREATE TABLE IF NOT EXISTS deletion_candidates (
    resource_kind TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    uid TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    evidence TEXT,
    PRIMARY KEY (resource_kind, namespace, name)
);

-- Recommendations produced by the previous rules (any LoadBalancer without
-- legacy Endpoints, any PV whose claim was missing) may be false positives:
-- retire them with an audit event. Later scans re-create the ones that the
-- evidence model confirms, after the minimum unused period.
INSERT INTO recommendation_events (recommendation_id, event_type, actor, payload, created_at)
SELECT id,
       'retired',
       'system',
       '{"version": 2, "details": {"code": "deletion_evidence_required", "from_status": "active", "to_status": "stale", "reason": "Retired on upgrade: deletion recommendations now require evidence that the resource is unused (no selector match, no EndpointSlice endpoints, no Gateway, Ingress or route reference, no StatefulSet or snapshot dependency) for DELETION_MIN_UNUSED_DAYS. GreenKube re-creates it if the evidence confirms it."}}',
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM recommendation_history
WHERE status = 'active' AND type IN ('ORPHANED_LOAD_BALANCER', 'ORPHANED_PERSISTENT_VOLUME');

UPDATE recommendation_history
SET status = 'stale', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE status = 'active' AND type IN ('ORPHANED_LOAD_BALANCER', 'ORPHANED_PERSISTENT_VOLUME');
