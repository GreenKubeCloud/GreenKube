### Added
- `/report/export?include_idle=true` (CSV and JSON) appends, for a cluster-wide export, one `idle` row (`__idle__`, the node idle share) and one `cluster_total` row (`__cluster_total__`, allocated + idle) per selected range, labelled in the new `row_kind` column (`allocated` on workload rows). Off by default, so existing exports keep their rows. Their values are empty or `null`, never zero, when the idle history does not cover the range (NRG-02, WP-05.3). The Report page has an "Include node idle share" checkbox.
- Prometheus gauges for the node idle share of the latest collection (`greenkube_cluster_idle_co2e_grams`, `greenkube_cluster_idle_embodied_co2e_grams`, `greenkube_cluster_idle_cost_dollars`, `greenkube_cluster_idle_energy_joules`) and the cluster totals (`greenkube_cluster_with_idle_*` = allocated + idle). The allocated series are unchanged; the idle and cluster-total series are absent when the idle history does not cover the snapshot (NRG-02).
- The dashboard summary cache (`/metrics/dashboard-summary`) keeps the idle share, the cluster totals and `idle_data_since` of its cluster-wide windows (migration 0250).
- The Footprint page shows the allocated totals, the node idle share and the cluster total side by side for CO₂e, cost and energy, with "not available" and the reason when the idle share is unknown.
- Each decided change measurement records `untrusted_cost_hours` and `baseline_untrusted_cost_hours`, the workload hours of each window without trusted cost (migration 0251, MEA-05). The API returns them with the measurements and the change detail page shows them. Measurements decided earlier keep `null` and a valid evidence digest.

### Changed
- `/report/export` rows carry a `row_kind` column (always `allocated` without `include_idle`).

### Fixed
- `/report/export` lost the sample on each 7-day chunk boundary (closed windows followed by a cursor one second later); chunks are now half-open.
