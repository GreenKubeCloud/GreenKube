<script>
	import { onDestroy } from 'svelte';
	import { selectedNamespace, selectedTimeRange } from '$lib/stores.js';
	import {
		getMetricsSummary,
		getTimeseries,
		getMetricsByNamespace,
		getTopPods,
		getActiveRecommendations,
		getRecommendationSavings,
		getDashboardSummary,
		getDashboardTimeseries,
		refreshDashboardSummary,
		getOpenPullRequests,
		listChanges
	} from '$lib/api.js';
	import { formatCO2, formatCost, formatEnergy, formatNumber, formatRelativeTime } from '$lib/utils/format.js';
	import { buildTimeseriesOption, buildMultiSeriesOption, buildNamespaceDonutOption, buildTopPodsOption } from '$lib/charts.js';
	import StatCard from '$lib/components/StatCard.svelte';
	import DataState from '$lib/components/DataState.svelte';
	import Filters from '$lib/components/Filters.svelte';
	import Chart from '$lib/components/Chart.svelte';
	import Card from '$lib/components/Card.svelte';
	import SavingsHero from '$lib/components/SavingsHero.svelte';
	import StatusPill from '$lib/components/StatusPill.svelte';
	import { describeExpectedValue, isReliabilityLane } from '$lib/utils/decision.js';
	import { buildSavingsOverview, deliveryHint, etaFrom, targetName, warmUpOf } from '$lib/overview.js';

	// Slugs served by the pre-computed summary / timeseries cache tables
	const PRECOMPUTED_SLUGS = new Set(['24h', '7d', '30d', '1y', 'ytd']);
	/** Top inbox items shown on the Overview (server EV order). */
	const TOP_COUNT = 5;

	let summary = null;
	let timeseries = [];
	let nsBreakdown = [];
	let topPods = [];
	let recommendations = [];
	let savings = null;
	let savingsError = '';
	let recoError = '';
	let inFlight = { openPrs: null, applied: null, measuring: null, more: false };
	let loading = true;
	let recoLoading = true;
	let refreshing = false;
	let error = null;
	let requestController = new AbortController();

	onDestroy(() => requestController.abort());

	$: params = { namespace: $selectedNamespace, last: $selectedTimeRange };
	$: if (params) loadData();

	/**
	 * Normalise a pre-computed TimeseriesCachePoint into the shape the chart
	 * builders expect (same as a TimeseriesPoint from /metrics/timeseries).
	 */
	function normaliseCachePoint(p) {
		return {
			timestamp: p.bucket_ts,
			co2e_grams: p.co2e_grams,
			embodied_co2e_grams: p.embodied_co2e_grams,
			total_cost: p.total_cost,
			joules: p.joules
		};
	}

	let loadSeq = 0;

	function loadData() {
		// Guard against overlapping loads (reactive params + manual refresh):
		// a slower response must never overwrite a newer one.
		const seq = ++loadSeq;
		requestController.abort();
		requestController = new AbortController();
		const { signal } = requestController;
		// Savings and the inbox lead the page; footprint charts are context.
		return Promise.all([loadDecisions(seq, signal), loadFootprint(seq, signal)]);
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
			// Realized savings to date: the whole ledger, not the footprint period.
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

	async function loadFootprint(seq, signal) {
		loading = true;
		error = null;
		try {
			const ns = $selectedNamespace || undefined;
			const last = $selectedTimeRange;

			// For pre-computed slugs: KPI cards from summary table,
			// chart data from timeseries cache table — both are instantaneous.
			// For other ranges (1h, 6h): fall back to on-demand endpoints.
			let summaryPromise, timeseriesPromise;
			if (PRECOMPUTED_SLUGS.has(last)) {
				summaryPromise = getDashboardSummary({ namespace: ns, signal }).then(
					(r) => r.windows?.[last] ?? getMetricsSummary({ namespace: ns, last, signal })
				);
				timeseriesPromise = getDashboardTimeseries({ windowSlug: last, namespace: ns, signal }).then(
					(r) => (r.points?.length ? r.points.map(normaliseCachePoint) : getTimeseries({
						namespace: ns, last, granularity: 'hour', signal
					}))
				);
			} else {
				summaryPromise = getMetricsSummary({ namespace: ns, last, signal });
				timeseriesPromise = getTimeseries({ namespace: ns, last, granularity: 'hour', signal });
			}

			// Lightweight SQL-aggregated endpoints for donut + top-pods.
			const [s, ts, nsData, topData] = await Promise.all([
				summaryPromise,
				timeseriesPromise,
				getMetricsByNamespace({ namespace: ns, last, signal }),
				getTopPods({ namespace: ns, last, limit: 10, signal })
			]);
			if (seq !== loadSeq) return;
			summary = s;
			timeseries = ts;
			nsBreakdown = nsData;
			topPods = topData;
		} catch (e) {
			if (seq === loadSeq && e.name !== 'AbortError') error = e.message;
		} finally {
			if (seq === loadSeq) loading = false;
		}
	}

	/** Trigger an on-demand summary + timeseries cache refresh then reload. */
	async function handleRefresh() {
		if (refreshing) return;
		refreshing = true;
		try {
			const ns = $selectedNamespace || undefined;
			await refreshDashboardSummary({ namespace: ns });
			// Give the background task a moment to complete before reloading
			await new Promise((r) => setTimeout(r, 1500));
			await loadData();
		} catch (e) {
			error = e.message;
		} finally {
			refreshing = false;
		}
	}

	$: overview = recoLoading ? null : buildSavingsOverview(savings, recommendations);
	$: topRecommendations = recommendations.slice(0, TOP_COUNT);
	$: warmUp = warmUpOf(recommendations);

	$: co2Option = timeseries.length
		? buildTimeseriesOption(timeseries, { valueKey: 'co2e_grams', label: 'CO₂ Emissions', unit: 'g', windowSlug: $selectedTimeRange })
		: null;

	$: multiOption = timeseries.length ? buildMultiSeriesOption(timeseries, { windowSlug: $selectedTimeRange }) : null;

	$: nsDonutOption = nsBreakdown.length
		? buildNamespaceDonutOption(nsBreakdown, { label: 'CO₂ by Namespace' })
		: null;

	$: topPodsOption = topPods.length
		? buildTopPodsOption(topPods, { label: 'Top Pods by CO₂', topN: 8 })
		: null;

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
			<button
				on:click={handleRefresh}
				disabled={refreshing}
				title="Refresh summary data"
				class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium
				       bg-dark-800 border border-dark-600/50 text-dark-300
				       hover:text-green-400 hover:border-green-500/50 transition-colors
				       disabled:opacity-40 disabled:cursor-not-allowed"
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 20 20"
					fill="currentColor"
					aria-hidden="true"
					class="w-4 h-4 {refreshing ? 'animate-spin' : ''}"
				>
					<path
						fill-rule="evenodd"
						d="M15.312 11.424a5.5 5.5 0 01-9.201 2.466l-.312-.311h2.433a.75.75 0 000-1.5H3.989a.75.75 0 00-.75.75v4.242a.75.75 0 001.5 0v-2.43l.31.31a7 7 0 0011.712-3.138.75.75 0 00-1.449-.389zm1.23-3.723a.75.75 0 00.219-.53V2.929a.75.75 0 00-1.5 0V5.36l-.31-.31A7 7 0 003.239 8.188a.75.75 0 101.448.389A5.5 5.5 0 0113.89 6.11l.311.31h-2.432a.75.75 0 000 1.5h4.243a.75.75 0 00.53-.219z"
						clip-rule="evenodd"
					/>
				</svg>
				{refreshing ? 'Refreshing…' : 'Refresh'}
			</button>
			<Filters />
		</div>
	</div>

	<!-- Savings: realized, pending verification and opportunity, never summed (K1) -->
	<SavingsHero {overview} loading={recoLoading} {savingsError} />

	<!-- Changes in flight -->
	<section aria-labelledby="pipeline-heading" class="card-compact" data-testid="pipeline-strip">
		<div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
			<h2 id="pipeline-heading" class="text-sm font-semibold text-dark-300 uppercase tracking-wider">Changes in flight</h2>
			<a href="/ledger" class="text-xs text-green-400 hover:text-green-300">Changes and measurements →</a>
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

	<!-- Footprint: supporting context -->
	<section aria-labelledby="footprint-heading" class="space-y-4">
		<h2 id="footprint-heading" class="text-lg font-semibold text-dark-100">Footprint</h2>
		<DataState {loading} {error} empty={!summary}>
			<div class="space-y-6">
				<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
					<StatCard
						label="Total CO₂ (Scope 2+3)"
						value={formatCO2(summary?.total_co2e_all_scopes ?? summary?.total_co2e_grams ?? 0)}
						icon="🌿"
						color="green"
					/>
					<StatCard label="Total Cost" value={formatCost(summary?.total_cost ?? 0)} icon="💰" color="blue" />
					<StatCard label="Total Energy" value={formatEnergy(summary?.total_energy_joules ?? 0)} icon="⚡" color="yellow" />
					<StatCard label="Active Pods" value={formatNumber(summary?.pod_count ?? 0)} icon="🔲" color="purple" />
				</div>

				{#if summary?.updated_at}
					<p class="text-xs text-dark-500 -mt-2 text-right">
						Summary last refreshed {formatRelativeTime(summary.updated_at)}
					</p>
				{/if}

				<div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
					<div class="card-compact text-center">
						<p class="stat-label">Scope 2 CO₂ (electricity)</p>
						<p class="stat-value text-lg">{formatCO2(summary?.total_co2e_grams ?? 0)}</p>
					</div>
					<div class="card-compact text-center">
						<p class="stat-label">Scope 3 CO₂ (hardware)</p>
						<p class="stat-value text-lg">{formatCO2(summary?.total_embodied_co2e_grams ?? 0)}</p>
					</div>
					<div class="card-compact text-center">
						<p class="stat-label">Namespaces</p>
						<p class="stat-value text-lg">{formatNumber(summary?.namespace_count ?? 0)}</p>
					</div>
					<div class="card-compact text-center">
						<p class="stat-label">Avg CO₂/Pod (Scope 2+3)</p>
						<p class="stat-value text-lg">
							{summary?.pod_count ? formatCO2((summary.total_co2e_all_scopes ?? summary.total_co2e_grams ?? 0) / summary.pod_count) : '—'}
						</p>
					</div>
				</div>

				<div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
					<Card title="CO₂ Emissions Over Time" icon="📈">
						{#if co2Option}
							<Chart option={co2Option} height="320px" />
						{:else}
							<div class="flex items-center justify-center h-80 text-dark-500">No data available</div>
						{/if}
					</Card>
					<Card title="Multi-Metric Trend" icon="📊">
						{#if multiOption}
							<Chart option={multiOption} height="320px" />
						{:else}
							<div class="flex items-center justify-center h-80 text-dark-500">No data available</div>
						{/if}
					</Card>
				</div>

				<div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
					<Card title="CO₂ by Namespace" icon="🗂️">
						{#if nsDonutOption}
							<Chart option={nsDonutOption} height="320px" />
						{:else}
							<div class="flex items-center justify-center h-80 text-dark-500">No data available</div>
						{/if}
					</Card>
					<Card title="Top Pods by CO₂" icon="🏆">
						{#if topPodsOption}
							<Chart option={topPodsOption} height="320px" />
						{:else}
							<div class="flex items-center justify-center h-80 text-dark-500">No data available</div>
						{/if}
					</Card>
				</div>
			</div>
		</DataState>
	</section>
</div>
