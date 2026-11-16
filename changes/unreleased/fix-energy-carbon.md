### Added
- **Embodied emissions provenance (NRG-01):** pod, container and node idle rows carry `embodied_source` (`boavizta`, `configured`, `fallback_per_vcpu`, `fallback_default`; migration 0180). Rows of a fallback are flagged estimated with an "Embodied emissions estimated" reason, and projections list `embodied_sources` in their assumptions.
- **Operator embodied value:** the node annotation `greenkube.io/embodied-kg` sets the whole-node embodied emissions of on-prem hardware and takes precedence over Boavizta.
- **Capacity-proportional embodied fallback:** `DEFAULT_EMBODIED_KG_PER_VCPU` (12 kg per vCPU, the Boavizta m5 family ratio) and `DEFAULT_EMBODIED_KG_PER_GIB` (0), Helm `config.boavizta.embodiedKgPerVcpu` and `config.boavizta.embodiedKgPerGib`. `DEFAULT_EMBODIED_EMISSIONS_KG` now applies only when the node capacity is unknown.

### Changed
- **Impact Model 4.0.0:** embodied emissions change for every node. Rows stored before keep the flat 100 kg per node with `embodied_source` NULL; run `greenkube recompute --since <raw retention>` to re-allocate raw rows. Hourly aggregates keep the old values.

### Fixed
- **Boavizta values are used (NRG-01):** the response model now matches the Boavizta API v1 (`impacts.gwp.embedded` and `use` are objects); before, every lookup failed and every node got the flat 100 kg fallback, unflagged. The lookup asks for `verbose=false` and no `duration`, so the value is the lifetime embedded GWP (24 kg for `aws/m5.large`). A failed lookup is stored in `instance_carbon_profiles` with its error and retried at most every 24 hours instead of on every collection cycle; on-prem providers (`unknown`, `kind`, `k3s`, ...) are never looked up.
- **Node snapshot versions no longer flip every cycle (NRG-07):** the collection no longer writes node snapshots; `embodied_emissions_kg` in `node_snapshots_scd` holds only the operator annotation. Migration 0181 clears the derived values stored there and collapses the identical consecutive versions they created. `greenkube recompute` resolves embodied emissions from the stored Boavizta profiles like the collection, so a recompute no longer zeroes embodied CO2e.
