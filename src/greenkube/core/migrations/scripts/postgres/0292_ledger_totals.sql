-- 0292: Materialized totals of the savings ledger v2 (LED-09).
--
-- The summary, the savings report and the lifetime counters summed every
-- entry on every call. savings_ledger_totals keeps the signed sum, the sum of
-- the positive and of the negative entries and the entry count per group. It
-- is updated in the same transaction as the append of the entries, so it never
-- disagrees with them. Windowed reads still prorate entries.
--
-- The entries themselves are not touched: the existing ones are counted
-- once below. basis and reason_group use the readers' rules (an entry stored
-- before migration 0160 has its basis inferred from its method, and the cost
-- of a reliability fix is grouped apart).
CREATE TABLE IF NOT EXISTS savings_ledger_totals (
    cluster_uid TEXT NOT NULL,
    namespace TEXT NOT NULL,
    recommendation_type TEXT NOT NULL,
    method TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    book TEXT NOT NULL,
    entry_kind TEXT NOT NULL,
    basis TEXT NOT NULL,
    reason_group TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL DEFAULT 0,
    positive DOUBLE PRECISION NOT NULL DEFAULT 0,
    negative DOUBLE PRECISION NOT NULL DEFAULT 0,
    entries BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (cluster_uid, namespace, recommendation_type, method, quantity, unit, book, entry_kind, basis, reason_group)
);

INSERT INTO savings_ledger_totals
    (cluster_uid, namespace, recommendation_type, method, quantity, unit, book, entry_kind, basis, reason_group,
     value, positive, negative, entries)
SELECT cluster_uid, namespace, recommendation_type, method, quantity, unit, book, entry_kind, basis, reason_group,
       SUM(value), SUM(CASE WHEN value > 0 THEN value ELSE 0 END), SUM(CASE WHEN value < 0 THEN value ELSE 0 END),
       COUNT(*)
FROM (
    SELECT cluster_uid, COALESCE(namespace, '') AS namespace, COALESCE(recommendation_type, '') AS recommendation_type,
           method, quantity, COALESCE(unit, '') AS unit, book, entry_kind,
           COALESCE(basis, CASE method WHEN 'projected' THEN 'billable_expected' WHEN 'realized_billed' THEN 'billed'
               WHEN 'user_declared' THEN 'declared' ELSE 'allocated' END) AS basis,
           CASE WHEN group_key LIKE '%cost_of_fix:%' THEN 'cost_of_fix' ELSE '' END AS reason_group,
           value
    FROM savings_ledger_entries
) AS grouped
WHERE 1 = 1
GROUP BY cluster_uid, namespace, recommendation_type, method, quantity, unit, book, entry_kind, basis, reason_group
ON CONFLICT DO NOTHING;
