# Progressive rollouts

GreenKube includes a deterministic progressive-rollout state machine. A rollout is a strictly increasing list of percentages (for example `10, 50, 100`) and a dwell time for each step. The rollout reconciler is deterministic and safe to run repeatedly:

* a step is applied once, then must be observed at the requested percentage;
* only a `healthy` verification observation after the dwell window advances;
* `unknown` and `inconclusive` observations pause progress;
* an `unhealthy` observation requests rollback and never advances the state;
* completion is terminal, so a replayed provider event cannot re-apply it.

`RolloutReconciler.reconcile()` returns the next `ProgressiveRollout` state and an action. It does not persist state, apply provider changes, or run as part of the recommendation/API workflow. A caller integrating this state machine must persist the returned state, execute `APPLY` or `ROLLBACK`, then supply the observed percentage and health on the next reconciliation.
