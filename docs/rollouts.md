# Progressive rollouts

GreenKube keeps progressive rollout state separate from provider adapters. A
rollout is a strictly increasing list of percentages (for example `10, 50,
100`) and a dwell time for each step. The rollout reconciler is deterministic
and safe to run repeatedly:

* a step is applied once, then must be observed at the requested percentage;
* only a `healthy` verification observation after the dwell window advances;
* `unknown` and `inconclusive` observations pause progress;
* an `unhealthy` observation requests rollback and never advances the state;
* completion is terminal, so a replayed provider event cannot re-apply it.

The controller persists the returned `ProgressiveRollout` after each
reconciliation. Provider adapters should treat `RolloutAction.APPLY` and
`ROLLBACK` as desired state, then report the observed percentage and health on
the next loop. They must not mutate rollout state themselves.
