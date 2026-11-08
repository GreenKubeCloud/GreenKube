### Added

- Impact Model v1 (`greenkube.impact`, model version `1.0.0`): value types (`WorkloadRef`, `WorkloadImpact`, `ImpactDelta`, `UnitPrices`), the `ReservationAwareAllocation` policy as pure, conservation-tested functions with an explicit `__idle__` workload per node, and `ImpactModel.project` for container request changes, workload and asset deletions, replica/schedule changes and node-pool consolidation (roadmap B2, partial: allocation is not yet wired into collection).
- Cost fallback chain for projections: each sample is priced from its observed OpenCost cost (its CPU/RAM split, or its total apportioned with OpenCost's CPU/RAM weights), then observed per-resource costs on the node, then a bundled `PricingCatalog` of on-demand list prices in the configured currency, then custom rates defaulting to the OpenCost default custom pricing (roadmap B3).
- `scripts/generate_pricing_catalog.py` and generated catalogs from public price lists: AWS (Price List Bulk API), Azure (Retail Prices API), OVHcloud and Scaleway (public catalogs). GCP stays empty because its catalog API requires a key (roadmap B3).
- Settings `IMPACT_PRICING_CPU_CORE_HOUR`, `IMPACT_PRICING_RAM_GIB_HOUR`, `IMPACT_PRICING_CURRENCY` (Helm `impact.pricing.*`), `IMPACT_REALIZATION_FACTOR` (`impact.realizationFactor`, default 1.0) and `CARBON_PRICE_PER_TONNE` (`impact.carbonPricePerTonne`, default 100). `ImpactDelta.value_annual` combines expected cost and carbon at that price (roadmap B, I2 groundwork).
- Recommendations carry an `impact` JSON projection (model version, cost and CO2e breakdown, capacity released, realization factor, unit prices and their source), persisted in `recommendation_history.impact` (migration 0046) and returned by the recommendations API (roadmap B4).
- Methodology reference `docs/methodology/impact-model.md`: allocation policy, formulas, cost chain, realization factor, carbon price, SCI mapping and limitations (roadmap B1).

### Changed

- Recommendation evidence reports the Impact Model as its savings method (`impact_model/<version>/<kind>`) instead of the retired `request_reduction_ratio` and `observed_cost_annualized` labels.
- Karpenter NodePool consolidation keeps enough nodes for both the CPU and the memory requests of the pool.
- Off-peak scaling recommendations now project savings (replica-hours removed in the idle window). Karpenter NodePool recommendations project the price and idle/embodied emissions of the removable nodes instead of `pool cost × (1 − utilization)`. Zombie and idle-namespace recommendations project CO2e from the allocated share (idle and embodied by reservation, dynamic by usage, capped at the node) instead of the usage-based row value.
- Without an analysis window, projections annualize over the observed span of the samples instead of reporting the raw window total.

### Fixed

- CPU and memory rightsizing (native analyzers and the VPA connector) no longer apply the request-cut ratio to the total pod cost: each resource is priced separately and anchored to the container's observed OpenCost cost, so CPU and memory recommendations on one container can no longer claim more than the container is charged (BUG-15, roadmap B4).
- Request-cut CO2e projections now come from the idle and embodied share released times the realization factor, instead of scaling usage-based emissions that requests do not change. Memory cuts project no CO2e until memory power is modeled (BUG-16, roadmap B4).
