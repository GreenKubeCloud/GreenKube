-- 0123: installation identity (COL-02, STO-09).
-- One row: the cluster this database schema belongs to. cluster_uid is the kube-system namespace UID
-- (source kube-system), or name:<CLUSTER_NAME> without Kubernetes access (source cluster_name).
-- Watermarks and collection window records are keyed by cluster_uid, so renaming CLUSTER_NAME or
-- moving the controller to another node never forks them. A controller of another cluster refuses
-- to collect into this schema unless ALLOW_CLUSTER_UID_CHANGE is set.

CREATE TABLE IF NOT EXISTS installation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cluster_uid TEXT NOT NULL,
    cluster_name TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
