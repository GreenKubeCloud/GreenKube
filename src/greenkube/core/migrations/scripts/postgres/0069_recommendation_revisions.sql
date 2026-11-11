-- 0069: Recommendation revisions and plan pins (roadmap E7) (PostgreSQL).
-- recommendation_revisions keeps every revision a recommendation no longer
-- carries: "superseded" rows hold the proposal a newer revision replaced, a
-- "queued" row holds advice computed while a plan was open on the record
-- (the record never changes under an open plan, the PR shows "newer advice
-- available"). At most one queued row per recommendation, promoted to the
-- record once the plan is closed.
-- recommendation_plan_pins records the revision each plan implements, so a
-- plan always refers to (recommendation_id, revision). The first pin of a
-- plan wins. Additive and idempotent.

CREATE TABLE IF NOT EXISTS recommendation_revisions (
    id BIGSERIAL PRIMARY KEY,
    recommendation_id BIGINT NOT NULL REFERENCES recommendation_history (id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('superseded', 'queued')),
    proposal TEXT,
    consensus TEXT,
    snapshot TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (recommendation_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_reco_revisions_state ON recommendation_revisions (state, recommendation_id);

CREATE TABLE IF NOT EXISTS recommendation_plan_pins (
    plan_id TEXT NOT NULL,
    recommendation_id BIGINT NOT NULL REFERENCES recommendation_history (id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    pinned_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (plan_id, recommendation_id)
);

CREATE INDEX IF NOT EXISTS idx_reco_plan_pins_recommendation ON recommendation_plan_pins (recommendation_id);
