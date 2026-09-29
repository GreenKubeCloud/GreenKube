-- 0020: Versioned runtime configuration overrides
CREATE TABLE IF NOT EXISTS configuration_overrides (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    values_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
