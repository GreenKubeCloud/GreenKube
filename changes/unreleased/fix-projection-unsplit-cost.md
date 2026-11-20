### Fixed
- Projected cost savings were up to several hundred times too low during the 14 days after an upgrade from 0.3.x (F-03). Hours re-aggregated from 0.3.x rows carry only `total_cost`, but the projection baseline divided the CPU and RAM cost of the newer hours by the duration of every hour. Each component is now priced over the hours that carry it, and the total of the other hours is apportioned at the node's CPU/RAM price ratio. Impact Model version 4.0.0: open recommendations are re-projected at the next scan; stored projections keep the version that made them, and allocated metric rows are unchanged.

### Added
- Projections record `assumptions.unsplit_cost_hours_share`, the share of observed hours whose cost had no CPU/RAM split and was apportioned, and the unit price `detail` `observed_components_partly_apportioned`.
