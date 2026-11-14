<script>
	import { formatCO2 } from '$lib/utils/format.js';
	import { formatMoney } from '$lib/utils/decision.js';
	import StatusPill from '$lib/components/StatusPill.svelte';

	/** Output of buildSavingsOverview ($lib/overview.js); null while unknown. */
	export let overview = null;
	export let loading = false;
	/** Error message when the savings report could not be read. */
	export let savingsError = '';

	$: currency = overview?.currency;
	$: realized = overview?.realized;
	$: pending = overview?.pending;
	$: opportunity = overview?.opportunity;

	function money(value) {
		return formatMoney(value ?? 0, currency);
	}
</script>

<section aria-labelledby="savings-heading" class="space-y-3">
	<div class="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
		<h2 id="savings-heading" class="text-lg font-semibold text-dark-100">Savings</h2>
		<p class="text-xs text-dark-500" data-testid="never-summed-note">
			Measured and projected savings are different quantities: they are shown side by side and never added together.
		</p>
	</div>

	<div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
		<!-- Realized (measured) -->
		<article class="card-compact flex flex-col gap-2 border-green-700/40" data-testid="savings-realized" aria-labelledby="realized-title">
			<div class="flex items-center justify-between gap-2">
				<h3 id="realized-title" class="stat-label text-xs">Realized savings</h3>
				<StatusPill label="Measured" tone="green" icon="✓" />
			</div>
			{#if loading}
				<p class="text-sm text-dark-500" role="status">Loading…</p>
			{:else if savingsError}
				<p class="text-sm text-amber-300" role="alert">{savingsError}</p>
			{:else if realized?.hasMeasurement}
				<p class="text-2xl font-bold text-green-400" data-testid="realized-cost">{money(realized.toDate?.cost)}</p>
				<p class="text-sm text-dark-200" data-testid="realized-co2e">{formatCO2(realized.toDate?.co2eGrams ?? 0)} CO₂e avoided</p>
				<p class="text-xs text-dark-500">Accrued in the savings ledger to date.</p>
				{#if realized.runRateAnnual}
					<p class="text-xs text-dark-400" data-testid="realized-run-rate">
						Measured run rate: {money(realized.runRateAnnual.cost)}/yr · {formatCO2(realized.runRateAnnual.co2eGrams)} CO₂e/yr
					</p>
				{/if}
			{:else}
				<p class="text-2xl font-bold text-dark-300">{money(0)}</p>
				<p class="text-xs text-dark-400" data-testid="realized-empty">
					No measured savings yet. A merged change is measured after its warm-up and measurement window, then its
					savings appear here.
				</p>
			{/if}
			<a href="/ledger" class="mt-auto text-xs text-green-400 hover:text-green-300">Open the savings ledger →</a>
		</article>

		<!-- Pending verification (projected, applied) -->
		<article class="card-compact flex flex-col gap-2" data-testid="savings-pending" aria-labelledby="pending-title">
			<div class="flex items-center justify-between gap-2">
				<h3 id="pending-title" class="stat-label text-xs">Pending verification</h3>
				<StatusPill label="Projected" tone="blue" icon="≈" />
			</div>
			{#if loading}
				<p class="text-sm text-dark-500" role="status">Loading…</p>
			{:else if pending?.annual && (pending.annual.cost || pending.annual.co2eGrams)}
				<p class="text-2xl font-bold text-blue-300" data-testid="pending-cost">{money(pending.annual.cost)}/yr</p>
				<p class="text-sm text-dark-200" data-testid="pending-co2e">{formatCO2(pending.annual.co2eGrams)} CO₂e/yr</p>
				<p class="text-xs text-dark-500">
					Projection for {pending.appliedCount} applied {pending.appliedCount === 1 ? 'change' : 'changes'} not measured yet.
				</p>
			{:else}
				<p class="text-2xl font-bold text-dark-300">{money(0)}/yr</p>
				<p class="text-xs text-dark-400" data-testid="pending-empty">No applied change is waiting for measurement.</p>
			{/if}
		</article>

		<!-- Opportunity (projected, not applied) -->
		<article class="card-compact flex flex-col gap-2" data-testid="savings-opportunity" aria-labelledby="opportunity-title">
			<div class="flex items-center justify-between gap-2">
				<h3 id="opportunity-title" class="stat-label text-xs">Opportunity</h3>
				<StatusPill label="Projected" tone="blue" icon="≈" />
			</div>
			{#if loading}
				<p class="text-sm text-dark-500" role="status">Loading…</p>
			{:else if opportunity?.count}
				<p class="text-2xl font-bold text-yellow-300" data-testid="opportunity-cost">{money(opportunity.cost)}/yr</p>
				<p class="text-sm text-dark-200" data-testid="opportunity-co2e">{formatCO2(opportunity.co2eGrams)} CO₂e/yr</p>
				<p class="text-xs text-dark-500">
					Projected savings of {opportunity.count} open {opportunity.count === 1 ? 'recommendation' : 'recommendations'}.
				</p>
				{#if opportunity.ev !== null}
					<p class="text-xs text-dark-400" data-testid="opportunity-ev">
						Risk-adjusted expected value ≈ {money(opportunity.ev)}/yr
					</p>
				{/if}
			{:else}
				<p class="text-2xl font-bold text-dark-300">{money(0)}/yr</p>
				<p class="text-xs text-dark-400" data-testid="opportunity-empty">No open savings recommendation.</p>
			{/if}
			{#if opportunity?.reliabilityCount}
				<p class="text-xs text-purple-300">
					{opportunity.reliabilityCount} reliability {opportunity.reliabilityCount === 1 ? 'recommendation adds' : 'recommendations add'} resources and
					{opportunity.reliabilityCount === 1 ? 'is' : 'are'} not counted as savings.
				</p>
			{/if}
			<a href="/recommendations" class="mt-auto text-xs text-green-400 hover:text-green-300">Review recommendations →</a>
		</article>
	</div>

	{#if overview?.diagnostics?.length}
		<ul class="space-y-1 text-xs text-dark-500" aria-label="Savings caveats">
			{#each overview.diagnostics as note}
				<li>• {note}</li>
			{/each}
		</ul>
	{/if}
</section>
