### Added
- `consensus.floor` (also in `evidence.consensus`): the native lower bound of a consensus (`source_id`, `signal_id`, `value`, `reason` `oom_kills`, `policy_value`, `applied`).

### Fixed
- A memory increase GreenKube proposes after OOM kills is now a lower bound under every consensus policy. Before, under `priority` (default order `vpa,karpenter,greenkube`), or `median`/`weighted` with three or more voters, a VPA, KRR, pushed or plugin proposal above the minimum change could replace it with a lower value (400 Mi request, GreenKube 600 Mi after OOM kills, VPA 450 Mi: `priority` kept 450 Mi). The OOM evidence only reached the consensus as reason text; the analyzer now marks the increase and the consensus clamps the value to it. Stored recommendations are corrected by the next scan; `safest` results are unchanged.
