-- 0031: Persist fenced singleton ownership for the plan-worker PR polling fallback (PostgreSQL).

CREATE TABLE IF NOT EXISTS gitops_pr_polling_lease (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    owner_id TEXT NOT NULL CHECK (length(btrim(owner_id)) > 0),
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
