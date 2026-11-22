<script>
	/**
	 * Footprint charts (K7): CO₂e, cost, energy and pods of the selected
	 * period, moved here from the Overview. Footprint data is analysed here and
	 * in the Report section (CSV and JSON exports); it is not exported to Prometheus.
	 */
	import { onDestroy } from 'svelte';
	import { currentUser, hasScope, selectedNamespace, selectedTimeRange } from '#lib/stores.js';
	import {
		getMetricsSummary,
		getTimeseries,
		getMetricsByNamespace,
		getTopPods,
		getDashboardSummary,
		getDashboardTimeseries,
		refreshDashboardSummary
	} from '#lib/api.js';
	import { formatCO2, formatCost, formatEnergy, formatNumber, formatRelativeTime } from '#lib/utils/format.js';
	import { buildTimeseriesOption, buildMultiSeriesOption, buildNamespaceDonutOption, buildTopPodsOption } from '#lib/charts.js';
	import { NOT_AVAILABLE, idleShareNote, idleShareRows, windowStart } from '#lib/idleShare.js';
	import StatCard from '#lib/components/StatCard.svelte';
	import DataState from '#lib/components/DataState.svelte';
	import Filters from '#lib/components/Filters.svelte';
	import Chart from '#lib/components/Chart.svelte';
	import Card from '#lib/components/Card.svelte';

	// Slugs served by the pre-computed summary / timeseries cache tables
	const PRECOMPUTED_SLUGS = new Set(['24h', '7d', '30d', '1y', 'ytd']);

	let summary = null;
	let timeseries = [];
	let nsBreakdown = [];
	let topPods = [];
	let loading = true;
	let refreshing = false;
	let error = null;
	let requestController = new AbortController();
	let loadSeq = 0;

	onDestroy(() => requestController.abort());

	$: params = { namespace: $selectedNamespace, last: $selectedTimeRange };
	$: if (params) loadFootprint();

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

	async function loadFootprint() {
		// A slower response must never overwrite a newer one.
		const seq = ++loadSeq;
		requestController.abort();
		requestController = new AbortController();
		const { signal } = requestController;
		loading = true;
		error = null;
		try {
			const ns = $selectedNamespace || undefined;
			const last = $selectedTimeRange;

			// Pre-computed slugs read the summary and timeseries cache tables;
			// other ranges (1h, 6h) use the on-demand endpoints.
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
			// Recomputing the cache is a write: a viewer only reloads what is there.
			if (!hasScope($currentUser, 'write')) {
				await loadFootprint();
				return;
			}
			const ns = $selectedNamespace || undefined;
			await refreshDashboardSummary({ namespace: ns });
			// Give the background task a moment to complete before reloading
			await new Promise((r) => setTimeout(r, 1500));
			await loadFootprint();
		} catch (e) {
			error = e.message;
		} finally {
			refreshing = false;
		}
	}

	$: idleRows = idleShareRows(summary);
	$: idleNote = idleShareNote(summary, $selectedNamespace, windowStart($selectedTimeRange));

	/** A value of the idle-share table, or "not available" when unknown. */
	function formatIdleValue(key, value) {
		if (value === null || value === undefined) return NOT_AVAILABLE;
		if (key === 'cost') return formatCost(value);
		if (key === 'energy') return formatEnergy(value);
		return formatCO2(value);
	}

	$: co2Option = timeseries.length
		? buildTimeseriesOption(timeseries, { valueKey: 'co2e_grams', label: 'CO₂ Emissions', unit: 'g', windowSlug: $selectedTimeRange })
		: null;
	$: multiOption = timeseries.length ? buildMultiSeriesOption(timeseries, { windowSlug: $selectedTimeRange }) : null;
	$: nsDonutOption = nsBreakdown.length ? buildNamespaceDonutOption(nsBreakdown, { label: 'CO₂ by Namespace' }) : null;
	$: topPodsOption = topPods.length ? buildTopPodsOption(topPods, { label: 'Top Pods by CO₂', topN: 8 }) : null;
</script>

<svelte:head><title>Footprint · GreenKube</title></svelte:head>

<div class="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Footprint</h1>
			<p class="text-sm text-dark-500 mt-1">
				Emissions, cost and energy of the selected period. For per-namespace and per-pod detail, build a report and export it as CSV or JSON.
			</p>
		</div>
		<div class="flex items-center gap-3 flex-wrap">
			<button
				type="button"
				on:click={handleRefresh}
				disabled={refreshing}
				title="Refresh summary data"
				class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium
				       bg-dark-800 border border-dark-600/50 text-dark-300
				       hover:text-green-400 hover:border-green-500/50 transition-colors
				       disabled:opacity-40 disabled:cursor-not-allowed"
			>
				<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" class="w-4 h-4 {refreshing ? 'animate-spin' : ''}">
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

	<DataState {loading} {error} empty={!summary}>
		<div class="space-y-6" data-testid="footprint-charts">
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

			<!-- WP-05.3 (NRG-02): the totals above are allocated to pods; the node idle share
			     (unreserved capacity) and the cluster total sit next to them, unknown when null. -->
			<section class="card-compact space-y-2" aria-labelledby="idle-share-heading" data-testid="idle-share">
				<h2 id="idle-share-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">
					Allocated, idle share and cluster total
				</h2>
				<div class="overflow-x-auto">
					<table class="w-full text-sm" aria-labelledby="idle-share-heading">
						<thead>
							<tr class="text-left text-xs text-dark-500">
								<th scope="col"></th>
								<th scope="col" class="py-1 pr-4">Allocated to pods</th>
								<th scope="col" class="py-1 pr-4">Node idle share</th>
								<th scope="col" class="py-1">Cluster total</th>
							</tr>
						</thead>
						<tbody>
							{#each idleRows as row (row.key)}
								<tr class="border-t border-dark-700/50">
									<th scope="row" class="py-1 pr-4 text-left font-normal text-dark-400">{row.label}</th>
									<td class="py-1 pr-4 text-dark-100">{formatIdleValue(row.key, row.allocated)}</td>
									<td class="py-1 pr-4 {row.idle === null ? 'text-dark-500 italic' : 'text-dark-100'}">
										{formatIdleValue(row.key, row.idle)}{#if row.idleShare !== null}<span class="text-xs text-dark-500"> ({Math.round(row.idleShare * 100)}%)</span>{/if}
									</td>
									<td class="py-1 {row.total === null ? 'text-dark-500 italic' : 'text-dark-100'}">{formatIdleValue(row.key, row.total)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
				{#if idleNote}<p class="text-xs text-dark-500">{idleNote}</p>{/if}
			</section>

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
</div>
