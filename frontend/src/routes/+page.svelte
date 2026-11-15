<script>
	import { onDestroy } from 'svelte';
	import { selectedNamespace } from '$lib/stores.js';
	import { getActiveRecommendations, getRecommendationSavings, getOpenPullRequests, listChanges } from '$lib/api.js';
	import { formatCO2, formatCost } from '$lib/utils/format.js';
	import Filters from '$lib/components/Filters.svelte';
	import SavingsHero from '$lib/components/SavingsHero.svelte';
	import StatusPill from '$lib/components/StatusPill.svelte';
	import { describeExpectedValue, isReliabilityLane } from '$lib/utils/decision.js';
	import { buildSavingsOverview, deliveryHint, etaFrom, targetName, warmUpOf } from '$lib/overview.js';

	/** Top inbox items shown on the Overview (server EV order). */
	const TOP_COUNT = 5;

	let recommendations = [];
	let savings = null;
	let savingsError = '';
	let recoError = '';
	let inFlight = { openPrs: null, applied: null, measuring: null, more: false };
	let recoLoading = true;
	let requestController = new AbortController();

	onDestroy(() => requestController.abort());

	// The footprint charts moved to /footprint (K7); the Overview reads decisions only.
	$: loadData($selectedNamespace);

	let loadSeq = 0;

	function loadData(_namespace) {
		// A slower response must never overwrite a newer one.
		const seq = ++loadSeq;
		requestController.abort();
		requestController = new AbortController();
		const { signal } = requestController;
		return loadDecisions(seq, signal);
	}

	/**
	 * Savings, ranked recommendations and changes in flight. Plain reads only:
	 * scans run on the controller schedule, never on page load (BUG-25).
	 */
	async function loadDecisions(seq, signal) {
		recoLoading = true;
		const ns = $selectedNamespace || undefined;
		const [recs, sav, prs, changes] = await Promise.allSettled([
			getActiveRecommendations({ namespace: ns, signal }),
			// Measured allocated savings to date: the whole ledger, not the footprint period.
			getRecommendationSavings({ namespace: ns, signal }),
			getOpenPullRequests({ signal }),
			listChanges({ namespace: ns, state: ['applied', 'measuring'], limit: 200, signal })
		]);
		if (seq !== loadSeq) return;
		recommendations = recs.status === 'fulfilled' && Array.isArray(recs.value) ? recs.value : [];
		recoError = recs.status === 'rejected' && recs.reason?.name !== 'AbortError' ? recs.reason?.message || 'Recommendations are unavailable.' : '';
		savings = sav.status === 'fulfilled' ? sav.value : null;
		savingsError = sav.status === 'rejected' && sav.reason?.name !== 'AbortError' ? 'Savings are temporarily unavailable.' : '';
		const changeItems = changes.status === 'fulfilled' && Array.isArray(changes.value?.items) ? changes.value.items : null;
		inFlight = {
			openPrs: prs.status === 'fulfilled' && Array.isArray(prs.value) ? prs.value.length : null,
			applied: changeItems ? changeItems.filter((item) => item.state === 'applied').length : null,
			measuring: changeItems ? changeItems.filter((item) => item.state === 'measuring').length : null,
			more: Boolean(changes.status === 'fulfilled' && changes.value?.next_cursor)
		};
		recoLoading = false;
	}

	$: overview = recoLoading ? null : buildSavingsOverview(savings, recommendations);
	$: topRecommendations = recommendations.slice(0, TOP_COUNT);
	$: warmUp = warmUpOf(recommendations);

	function countText(value, more = false) {
		if (value === null || value === undefined) return '—';
		return more ? `${value}+` : String(value);
	}
</script>

<svelte:head><title>Overview · GreenKube</title></svelte:head>

<div class="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<!-- Header -->
	<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Overview</h1>
			<p class="text-sm text-dark-500 mt-1">
				What GreenKube saved, what it could still save, and what to do next
			</p>
		</div>
		<div class="flex items-center gap-3 flex-wrap">
			<Filters showTimeRange={false} />
		</div>
	</div>

	<!-- Savings: measured allocated, pending verification and opportunity, never summed (K1) -->
	<SavingsHero {overview} loading={recoLoading} {savingsError} />

	<!-- Changes in flight -->
	<section aria-labelledby="pipeline-heading" class="card-compact" data-testid="pipeline-strip">
		<div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
			<h2 id="pipeline-heading" class="text-sm font-semibold text-dark-300 uppercase tracking-wider">Changes in flight</h2>
			<a href="/changes" class="text-xs text-green-400 hover:text-green-300">Changes board →</a>
		</div>
		<dl class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
			<div>
				<dt class="text-xs text-dark-500">Open pull requests</dt>
				<dd class="text-xl font-semibold text-dark-100" data-testid="inflight-prs">{recoLoading ? '…' : countText(inFlight.openPrs)}</dd>
			</div>
			<div>
				<dt class="text-xs text-dark-500">Applied, waiting for measurement</dt>
				<dd class="text-xl font-semibold text-dark-100" data-testid="inflight-applied">
					{recoLoading ? '…' : countText(inFlight.applied, inFlight.more)}
				</dd>
			</div>
			<div>
				<dt class="text-xs text-dark-500">Measuring</dt>
				<dd class="text-xl font-semibold text-dark-100" data-testid="inflight-measuring">
					{recoLoading ? '…' : countText(inFlight.measuring, inFlight.more)}
				</dd>
			</div>
		</dl>
	</section>

	<!-- What to do next: top recommendations in server order (EV, I1) -->
	<section aria-labelledby="inbox-heading" class="card">
		<div class="flex items-center justify-between gap-3 mb-4">
			<h2 id="inbox-heading" class="text-sm font-semibold text-dark-300 uppercase tracking-wider">What to do next</h2>
			<a href="/recommendations" class="text-sm text-green-400 hover:text-green-300 transition-colors">View all →</a>
		</div>
		{#if recoLoading}
			<p class="text-sm text-dark-500" role="status">Loading recommendations…</p>
		{:else if recoError}
			<p class="text-sm text-amber-300" role="alert">{recoError}</p>
		{:else if !recommendations.length}
			<div class="text-sm text-dark-400 space-y-1" data-testid="inbox-empty">
				<p>No open recommendation.</p>
				<p class="text-xs text-dark-500">
					Scans run on the controller schedule. Right after an install, GreenKube needs some usage history before it
					recommends anything; the <a href="/setup" class="text-green-400 hover:text-green-300">setup checklist</a> shows
					what is still missing.
				</p>
			</div>
		{:else}
			{#if warmUp}
				<p class="mb-3 rounded-lg border border-blue-700/40 bg-blue-900/20 px-3 py-2 text-xs text-blue-200" role="status" data-testid="warm-up">
					Collecting evidence: {warmUp.waiting} {warmUp.waiting === 1 ? 'recommendation becomes' : 'recommendations become'}
					eligible for a pull request in {etaFrom(warmUp.eligibleAt) ?? 'a moment'}.
				</p>
			{/if}
			<ol class="divide-y divide-dark-700/50">
				{#each topRecommendations as rec, index (rec.id ?? index)}
					{@const hint = deliveryHint(rec)}
					{@const evText = describeExpectedValue(rec)}
					<li class="py-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:gap-3">
						<span class="text-xs text-dark-500 w-5 shrink-0" aria-hidden="true">{index + 1}.</span>
						<div class="flex-1 min-w-0">
							<div class="flex items-center gap-2 flex-wrap">
								{#if rec.id != null}
									<a href="/recommendations/{rec.id}" class="text-sm font-medium text-dark-100 hover:text-green-300 truncate">
										{targetName(rec)}
									</a>
								{:else}
									<span class="text-sm font-medium text-dark-100 truncate">{targetName(rec)}</span>
								{/if}
								{#if rec.namespace}<span class="text-xs text-dark-500">{rec.namespace}</span>{/if}
								{#if isReliabilityLane(rec)}<StatusPill label="Reliability" tone="purple" />{/if}
							</div>
							<p class="text-xs text-dark-500 mt-0.5">{rec.reason || rec.description || ''}</p>
							{#if hint}
								<p class="text-xs mt-1 {hint.ready ? 'text-green-400' : 'text-dark-400'}" data-testid="delivery-hint">
									{hint.ready ? '✓' : '•'} {hint.text}
								</p>
							{/if}
						</div>
						<div class="sm:text-right shrink-0">
							{#if evText}
								<p class="text-xs text-dark-200 whitespace-nowrap" data-testid="dashboard-expected-value">{evText}</p>
							{/if}
							{#if rec.potential_savings_cost}
								<p class="text-xs text-dark-400 whitespace-nowrap">Projected {formatCost(rec.potential_savings_cost)}/yr</p>
							{:else if rec.projected_allocated_cost_annual > 0}
								<!-- IMP-03: fixed nodes bill nothing back; show the allocated figure with its note. -->
								<p class="text-xs text-dark-400 whitespace-nowrap" data-testid="dashboard-allocated-saving">Allocated {formatCost(rec.projected_allocated_cost_annual)}/yr</p>
								{#if rec.projection_note}<p class="text-[10px] text-dark-500">{rec.projection_note}</p>{/if}
							{/if}
							{#if rec.potential_savings_co2e_grams}
								<p class="text-xs text-green-400 whitespace-nowrap">−{formatCO2(rec.potential_savings_co2e_grams)} CO₂e/yr</p>
							{/if}
						</div>
					</li>
				{/each}
			</ol>
		{/if}
	</section>

	<!-- Footprint: supporting context, moved to /footprint (K7) -->
	<section aria-labelledby="footprint-heading" class="card-compact flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between" data-testid="footprint-link">
		<div>
			<h2 id="footprint-heading" class="text-sm font-semibold text-dark-300 uppercase tracking-wider">Footprint</h2>
			<p class="text-xs text-dark-500 mt-0.5">Emissions, cost and energy charts, nodes and reports.</p>
		</div>
		<a href="/footprint" class="text-sm text-green-400 hover:text-green-300">Open the footprint →</a>
	</section>
</div>
