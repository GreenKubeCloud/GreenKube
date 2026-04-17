<script>
	import { onMount } from 'svelte';
	import { selectedNamespace, selectedTimeRange } from '$lib/stores.js';
	import {
		getMetricsSummary,
		getTimeseries,
		getMetricsByNamespace,
		getTopPods,
		getRecommendations,
		getDashboardSummary,
		getDashboardTimeseries,
		refreshDashboardSummary
	} from '$lib/api.js';
	import { formatCO2, formatCost, formatEnergy, formatNumber, formatRelativeTime } from '$lib/utils/format.js';
	import { buildTimeseriesOption, buildMultiSeriesOption, buildNamespaceDonutOption, buildTopPodsOption } from '$lib/charts.js';
	import StatCard from '$lib/components/StatCard.svelte';
	import DataState from '$lib/components/DataState.svelte';
	import Filters from '$lib/components/Filters.svelte';
	import Chart from '$lib/components/Chart.svelte';
	import Card from '$lib/components/Card.svelte';

	// Slugs served by the pre-computed summary / timeseries cache tables
	const PRECOMPUTED_SLUGS = new Set(['24h', '7d', '30d', '1y', 'ytd']);

	let summary = null;
	let timeseries = [];
	let nsBreakdown = [];
	let topPods = [];
	let recommendations = [];
	let loading = true;
	let recoLoading = true;
	let refreshing = false;
	let error = null;

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

	async function loadData() {
		loading = true;
		recoLoading = true;
		error = null;
		try {
			const ns = $selectedNamespace || undefined;
			const last = $selectedTimeRange;

			// For pre-computed slugs: KPI cards from summary table,
			// chart data from timeseries cache table — both are instantaneous.
			// For other ranges (1h, 6h): fall back to on-demand endpoints.
			let summaryPromise, timeseriesPromise;
			if (PRECOMPUTED_SLUGS.has(last)) {
				summaryPromise = getDashboardSummary({ namespace: ns }).then(
					(r) => r.windows?.[last] ?? null
				);
				timeseriesPromise = getDashboardTimeseries({ windowSlug: last, namespace: ns }).then(
					(r) => (r.points ?? []).map(normaliseCachePoint)
				);
			} else {
				summaryPromise = getMetricsSummary({ namespace: ns, last });
				timeseriesPromise = getTimeseries({
					namespace: ns,
					last,
					granularity: 'hour'  // 1h and 6h are both sub-day → hourly buckets
				});
			}

			// Use lightweight SQL-aggregated endpoints for donut + top-pods
			// instead of loading all raw metrics into memory.
			const apiLast = last === 'ytd' ? '1y' : last;

			const [s, ts, nsData, topData] = await Promise.all([
				summaryPromise,
				timeseriesPromise,
				getMetricsByNamespace({ namespace: ns, last: apiLast }),
				getTopPods({ namespace: ns, last: apiLast, limit: 10 })
			]);
			summary = s;
			timeseries = ts;
			nsBreakdown = nsData;
			topPods = topData;
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}

		// Fetch recommendations in the background — they are slower and not
		// required for the initial render of KPI cards and charts.
		try {
			const ns = $selectedNamespace || undefined;
			recommendations = await getRecommendations({ namespace: ns });
		} catch {
			recommendations = [];
		} finally {
			recoLoading = false;
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

	onMount(() => loadData());
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<!-- Header -->
	<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Dashboard</h1>
			<p class="text-sm text-dark-500 mt-1">
				Environmental impact overview of your Kubernetes cluster
			</p>
		</div>
		<div class="flex items-center gap-3">
			<!-- Refresh button -->
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

	<DataState {loading} {error} empty={!summary}>
		<!-- KPI Row -->
		<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
			<StatCard
				label="Total CO₂ (Scope 2+3)"
				value={formatCO2(summary?.total_co2e_all_scopes ?? summary?.total_co2e_grams ?? 0)}
				icon="🌿"
				color="green"
			/>
			<StatCard
				label="Total Cost"
				value={formatCost(summary?.total_cost ?? 0)}
				icon="💰"
				color="blue"
			/>
			<StatCard
				label="Total Energy"
				value={formatEnergy(summary?.total_energy_joules ?? 0)}
				icon="⚡"
				color="yellow"
			/>
			<StatCard
				label="Active Pods"
				value={formatNumber(summary?.pod_count ?? 0)}
				icon="🔲"
				color="purple"
			/>
		</div>

		<!-- Last refreshed hint (only shown for pre-computed windows) -->
		{#if summary?.updated_at}
			<p class="text-xs text-dark-500 -mt-2 text-right">
				Summary last refreshed {formatRelativeTime(summary.updated_at)}
			</p>
		{/if}

		<!-- Additional Stats -->
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
			<div class="card-compact text-center">
				<p class="stat-label">Recommendations</p>
				<p class="stat-value text-lg {recommendations.length > 0 ? 'text-yellow-400' : ''}">
					{#if recoLoading}
						<span class="text-dark-500 text-sm">…</span>
					{:else}
						{recommendations.length}
					{/if}
				</p>
			</div>
		</div>

		<!-- Charts Row 1 -->
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

		<!-- Charts Row 2 -->
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

		<!-- Recommendations preview -->
		{#if recommendations.length > 0}
			<Card title="Recent Recommendations" icon="💡">
				<div slot="actions">
					<a href="/recommendations" class="text-sm text-green-400 hover:text-green-300 transition-colors">
						View all →
					</a>
				</div>
				<div class="divide-y divide-dark-700/50">
					{#each recommendations.slice(0, 3) as rec}
						<div class="py-3 flex items-start gap-3">
							<span class="badge-{rec.type === 'ZOMBIE_POD' ? 'red' : 'yellow'} mt-0.5">
								{rec.type === 'ZOMBIE_POD' ? '💀' : '📐'}
							</span>
							<div class="flex-1 min-w-0">
								<p class="text-sm text-dark-200 font-medium truncate">{rec.pod_name}</p>
								<p class="text-xs text-dark-500 mt-0.5">{rec.reason}</p>
							</div>
							{#if rec.potential_savings_co2e_grams}
								<span class="text-xs text-green-400 whitespace-nowrap">
									-{formatCO2(rec.potential_savings_co2e_grams)}
								</span>
							{/if}
						</div>
					{/each}
				</div>
			</Card>
		{/if}
	</DataState>
</div>
