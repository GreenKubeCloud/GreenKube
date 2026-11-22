/** Deep link target of PRs (J1): `/recommendations/{id}`. */
export function load({ params }) {
	return { id: params.id };
}
