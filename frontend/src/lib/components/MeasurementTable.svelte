<script>
	import { formatCO2, formatDate } from '$lib/utils/format.js';
	import { formatMoney } from '$lib/utils/decision.js';
	import { measurementRows } from '$lib/changes.js';
	import StatusPill from '$lib/components/StatusPill.svelte';

	/** One decided measurement from GET /api/v2/changes/{id}/measurements. */
	export let measurement;
	/** ISO 4217 code; undefined uses the pricing currency from GET /config. */
	export let currency = undefined;

	const OUTCOME_TONES = { realized: 'green', not_realized: 'yellow', inconclusive: 'dark', contaminated: 'yellow' };

	$: rows = measurementRows(measurement);

	function amount(row, value) {
		if (value === null) return '—';
		if (row.kind === 'cost') return formatMoney(value, currency);
		if (row.kind === 'co2e') return formatCO2(value);
		return `${value.toFixed(1)} ${row.unit}`;
	}
</script>

<div class="space-y-2" data-testid="measurement">
	<div class="flex flex-wrap items-center gap-2 text-xs text-dark-400">
		<StatusPill label={String(measurement?.outcome ?? 'unknown').replaceAll('_', ' ')} tone={OUTCOME_TONES[measurement?.outcome] ?? 'dark'} />
		<span>Decided <time datetime={measurement?.decided_at}>{formatDate(measurement?.decided_at)}</time></span>
		{#if measurement?.strategy_key}<span>· {measurement.strategy_key} {measurement.strategy_version ?? ''}</span>{/if}
		{#if measurement?.impact_model_version}<span>· Impact Model {measurement.impact_model_version}</span>{/if}
	</div>
	{#if rows.length}
		<div class="overflow-x-auto">
			<table class="w-full min-w-[480px] text-xs">
				<caption class="sr-only">Measured savings per quantity, annualized, with a 90% confidence interval</caption>
				<thead>
					<tr class="text-left text-dark-500">
						<th scope="col" class="py-1 font-medium">Quantity</th>
						<th scope="col" class="py-1 font-medium">Measured /yr</th>
						<th scope="col" class="py-1 font-medium">90% interval</th>
						<th scope="col" class="py-1 font-medium">Projected /yr</th>
						<th scope="col" class="py-1 font-medium">Outcome</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as row (row.quantity)}
						<tr class="border-t border-dark-700/50 {row.claimed ? '' : 'text-dark-500'}">
							<th scope="row" class="py-1 text-left font-normal {row.claimed ? 'text-dark-200' : ''}">
								{row.label}{#if !row.claimed}<span class="sr-only"> (reported, not claimed)</span>{/if}
							</th>
							<td class={row.claimed ? 'text-dark-100' : ''}>{amount(row, row.annualized)}</td>
							<td>{row.ciLow === null ? '—' : `${amount(row, row.ciLow)} … ${amount(row, row.ciHigh)}`}</td>
							<td>{amount(row, row.projectedAnnual)}</td>
							<td>{row.outcome ? row.outcome.replaceAll('_', ' ') : '—'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		<p class="text-[11px] text-dark-500">
			Measured values are the realized savings; the projected column is the original estimate, shown for comparison only.
		</p>
	{/if}
	{#if measurement?.reasons?.length}
		<ul class="text-xs text-dark-400 list-disc pl-4">
			{#each measurement.reasons as reason}<li>{reason}</li>{/each}
		</ul>
	{/if}
</div>
