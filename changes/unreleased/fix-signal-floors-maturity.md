### Added
- `SIGNAL_TOOL_FLOORS` (`sources.toolFloors`): the known minimum of each tool per engine family or source id (defaults: KRR `simple` 10m and 100 Mi, Fairwinds VPA chart 15m and 100 Mi per pod, split between containers). A proposal on a tool floor is not counted as a consensus vote.
- `SIGNAL_MIN_CHANGE_PERCENT`, `SIGNAL_MIN_CHANGE_CPU_MILLICORES`, `SIGNAL_MIN_CHANGE_MEMORY_BYTES` (`sources.minChange`): an external proposal that changes the request by less than 5 % or 10m CPU / 16 MiB is not a vote.
- `VPA_MIN_SAMPLE_HOURS` (`sources.vpa.minSampleHours`, default 24): the VPA source reads `status.conditions` and the `VerticalPodAutoscalerCheckpoint` sample history (`totalSamplesCount`, `firstSampleStart`, `lastSampleStart`) and ignores a VPA until it is mature (`immature_vpas` diagnostic; `vpa_maturity_unknown` warning when checkpoints cannot be read). The chart ClusterRole now grants `get`/`list` on `verticalpodautoscalercheckpoints`.
- `consensus.ignored_votes` lists the external proposals set aside (`tool_floor`, `below_min_change`).

### Fixed
- Tool floors no longer win the `safest` consensus over a measured native value (F-12): seaweedfs-volume measured 42 Mi, VPA and KRR proposed their 100 Mi floor, and 100 Mi was retained; the native value plus headroom is now retained.
- An external source alone (VPA, Goldilocks, KRR, push), with no native GreenKube proposal for the container, no longer creates an active container-resources recommendation (78 KRR-only recommendations in the field). Its advice stays a signal on the Sources page. Existing source-only records are no longer re-emitted and are reconciled as stale by the next complete scan.
