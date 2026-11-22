/** Deep link target of PRs (J1): `/changes/{plan_id}`. */
export function load({ params }) {
	return { planId: params.plan_id };
}
