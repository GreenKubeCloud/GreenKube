<script>
	/**
	 * Released capacity block (WP-08.1): reserved capacity in its own units,
	 * labelled projected or measured, never money and never a saving.
	 */
	import StatusPill from '$lib/components/StatusPill.svelte';
	import { releasedCapacityOf } from '$lib/releasedCapacity.js';

	export let capacity = null;
	export let testid = 'released-capacity';

	const VERDICT_LABELS = {
		released: 'Released',
		partially_released: 'Partially released',
		not_released: 'Not released',
		replica_change_only: 'Replica change only',
		unknown: 'Unknown'
	};

	$: view = releasedCapacityOf(capacity);
</script>

{#if view}
	<section class="card space-y-3" aria-labelledby="{testid}-heading" data-testid={testid}>
		<div class="flex flex-wrap items-center justify-between gap-2">
			<h2 id="{testid}-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">
				Released capacity ({view.method})
			</h2>
			<div class="flex items-center gap-2">
				<StatusPill label={view.label} tone={view.method === 'measured' ? 'green' : 'blue'} icon={view.method === 'measured' ? '✓' : '≈'} />
				{#if view.verdict}<span class="text-xs text-dark-300" data-testid="{testid}-verdict">{VERDICT_LABELS[view.verdict] ?? view.verdict}</span>{/if}
				{#if view.period}<span class="text-xs text-dark-500">{view.period}</span>{/if}
			</div>
		</div>
		{#if view.rows.length}
			<dl class="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
				{#each view.rows as row (row.key)}
					<div><dt class="text-dark-500">{row.label}</dt><dd class={row.value < 0 ? 'text-amber-300' : 'text-dark-100'}>{row.text}</dd></div>
				{/each}
			</dl>
		{/if}
		{#if view.replicaChange}
			<p class="text-xs text-dark-400" data-testid="{testid}-replicas">
				Replica change reported separately, not booked as a release: {view.replicaChange.before ?? '—'} → {view.replicaChange.after ?? '—'} replicas.
			</p>
		{/if}
		<p class="text-xs text-dark-500">{view.increase ? 'Increase: more capacity is reserved. ' : ''}{view.note}</p>
	</section>
{/if}
