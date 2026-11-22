/** Deep link target of PRs (J1): `/ledger?change={change_id or plan_id}`. */
export function load({ url }) {
	return { change: url.searchParams.get('change') || null };
}
