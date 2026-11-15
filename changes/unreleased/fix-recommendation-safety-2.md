### Added
- Advice inserted for a container after its rightsizing change was recorded names that change: `previous_recommendation_id` and `previous_change_id` on the recommendation (migration 0131, SQLite and PostgreSQL), so the UI and the ledger can relate an OOM-driven increase to the decrease it follows (REC-01).

### Fixed
- The run diff no longer reports as `new` a record the repository did not write: `upsert_recommendations` reports one outcome per record (`inserted`, `revised`, `refreshed`, `queued`, `kept`, or not written: `absorbed`, `pr_open`, `applied`, `ignored`) and returns only the number of records that have an active row; the scan's persisted recommendations, `recommendation_count` and `new_count` count only those (REC-01).
