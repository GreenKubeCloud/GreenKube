<script>
	import { onMount } from 'svelte';
	import { getNamespaces, getReportSummary, buildReportExportUrl } from '$lib/api.js';
	import { formatCO2, formatCost, formatEnergy } from '$lib/utils/format.js';
	import Card from '$lib/components/Card.svelte';
	import StatCard from '$lib/components/StatCard.svelte';
	import DataState from '$lib/components/DataState.svelte';

	// ── Filter state ──────────────────────────────────────────────────────────
	let namespace = '';
	let last = '24h';
	let aggregate = false;
	let granularity = 'daily';
	let format = 'csv';

	// ── UI state ──────────────────────────────────────────────────────────────
	let namespaces = [];
	let summary = null;
	let loading = false;
	let error = null;
	let downloading = false;
	let downloadError = null;
	let downloadSuccess = false;

	const timeRanges = [
		{ value: '1h',  label: '1 hour' },
		{ value: '6h',  label: '6 hours' },
		{ value: '24h', label: '24 hours' },
		{ value: '7d',  label: '7 days' },
		{ value: '30d', label: '30 days' },
		{ value: '90d', label: '90 days' },
		{ value: '1y',  label: '1 year' }
	];

	const granularities = [
		{ value: 'hourly',   label: 'Hourly' },
		{ value: 'daily',    label: 'Daily' },
		{ value: 'weekly',   label: 'Weekly' },
		{ value: 'monthly',  label: 'Monthly' },
		{ value: 'yearly',   label: 'Yearly' }
	];

	const formats = [
		{ value: 'csv',  label: 'CSV', icon: '📊', description: 'Spreadsheet-compatible, ideal for Excel / Google Sheets' },
		{ value: 'json', label: 'JSON', icon: '🗂️', description: 'Machine-readable, ideal for further processing' }
	];

	// ── Reactive summary preview ──────────────────────────────────────────────
	$: previewParams = { namespace, last, aggregate, granularity: aggregate ? granularity : undefined };
	$: if (previewParams) refreshSummary();

	async function refreshSummary() {
		loading = true;
		error = null;
		summary = null;
		try {
			summary = await getReportSummary({
				namespace: namespace || undefined,
				last,
				aggregate,
				granularity: aggregate ? granularity : undefined
			});
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	async function handleExport() {
		downloading = true;
		downloadError = null;
		downloadSuccess = false;
		try {
			const url = buildReportExportUrl({
				namespace: namespace || undefined,
				last,
				aggregate,
				granularity: aggregate ? granularity : undefined,
				format
			});
			// Trigger browser download without leaving the page
			const a = document.createElement('a');
			a.href = url;
			a.download = '';
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
			downloadSuccess = true;
			setTimeout(() => { downloadSuccess = false; }, 4000);
		} catch (e) {
			downloadError = e.message;
		} finally {
			downloading = false;
		}
	}

	onMount(async () => {
		try {
			namespaces = await getNamespaces();
		} catch {
			namespaces = [];
		}
		await refreshSummary();
	});
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1200px] mx-auto">

	<!-- ── Header ── -->
	<div>
		<h1 class="text-2xl font-bold text-dark-100">Report</h1>
		<p class="text-sm text-dark-500 mt-1">
			Configure, preview and export your FinGreenOps report as CSV or JSON.
		</p>
	</div>

	<div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

		<!-- ── Left panel: configuration ── -->
		<div class="lg:col-span-1 space-y-4">

			<!-- Time range -->
			<Card title="Time Range">
				<div class="space-y-3">
					<div class="grid grid-cols-2 gap-2">
						{#each timeRanges as tr}
							<button
								on:click={() => (last = tr.value)}
								class="px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150
								       {last === tr.value
										? 'bg-green-600/20 text-green-400 border border-green-600/50'
										: 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:text-dark-200 hover:bg-dark-700'}"
							>
								{tr.label}
							</button>
						{/each}
					</div>
				</div>
			</Card>

			<!-- Namespace filter -->
			<Card title="Namespace">
				<select
					bind:value={namespace}
					class="w-full bg-dark-800 border border-dark-600/50 text-dark-200 text-sm rounded-lg px-3 py-2
					       focus:ring-2 focus:ring-green-500/50 focus:border-green-500 transition-colors"
				>
					<option value="">All namespaces</option>
					{#each namespaces as ns}
						<option value={ns}>{ns}</option>
					{/each}
				</select>
			</Card>

			<!-- Aggregation -->
			<Card title="Aggregation">
				<div class="space-y-3">
					<label class="flex items-center gap-3 cursor-pointer group">
						<div class="relative">
							<input
								type="checkbox"
								bind:checked={aggregate}
								class="sr-only peer"
							/>
							<div class="w-10 h-5 bg-dark-700 rounded-full peer peer-checked:bg-green-600 transition-colors"></div>
							<div class="absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform peer-checked:translate-x-5"></div>
						</div>
						<span class="text-sm text-dark-300 group-hover:text-dark-100 transition-colors">
							Aggregate by period
						</span>
					</label>

					{#if aggregate}
						<div class="space-y-1.5 pt-1">
							<p class="text-xs text-dark-500 uppercase tracking-wider font-medium">Grouping</p>
							<div class="grid grid-cols-1 gap-1.5">
								{#each granularities as g}
									<button
										on:click={() => (granularity = g.value)}
										class="px-3 py-1.5 rounded-lg text-sm text-left transition-all duration-150
										       {granularity === g.value
												? 'bg-green-600/20 text-green-400 border border-green-600/50'
												: 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:text-dark-200 hover:bg-dark-700'}"
									>
										{g.label}
									</button>
								{/each}
							</div>
						</div>
					{/if}
				</div>
			</Card>

			<!-- Export format -->
			<Card title="Export Format">
				<div class="space-y-2">
					{#each formats as f}
						<button
							on:click={() => (format = f.value)}
							class="w-full flex items-start gap-3 px-3 py-2.5 rounded-lg text-left transition-all duration-150
							       {format === f.value
									? 'bg-green-600/20 border border-green-600/50'
									: 'bg-dark-800 border border-dark-700/50 hover:bg-dark-700'}"
						>
							<span class="text-xl mt-0.5">{f.icon}</span>
							<div>
								<p class="text-sm font-medium {format === f.value ? 'text-green-400' : 'text-dark-200'}">
									{f.label}
								</p>
								<p class="text-xs text-dark-500 mt-0.5">{f.description}</p>
							</div>
						</button>
					{/each}
				</div>
			</Card>

		</div>

		<!-- ── Right panel: preview + download ── -->
		<div class="lg:col-span-2 space-y-4">

			<!-- Preview summary -->
			<Card title="Report Preview">
				<DataState {loading} {error} empty={!summary || summary.total_rows === 0} emptyMessage="No data found for the selected filters. Try a longer time range or a different namespace.">
					<div class="grid grid-cols-2 sm:grid-cols-3 gap-3">
						<div class="bg-dark-800 rounded-lg p-3 text-center">
							<p class="text-xs text-dark-500 uppercase tracking-wider mb-1">Rows</p>
							<p class="text-xl font-bold text-dark-100">{summary?.total_rows ?? 0}</p>
						</div>
						<div class="bg-dark-800 rounded-lg p-3 text-center">
							<p class="text-xs text-dark-500 uppercase tracking-wider mb-1">Pods</p>
							<p class="text-xl font-bold text-dark-100">{summary?.unique_pods ?? 0}</p>
						</div>
						<div class="bg-dark-800 rounded-lg p-3 text-center">
							<p class="text-xs text-dark-500 uppercase tracking-wider mb-1">Namespaces</p>
							<p class="text-xl font-bold text-dark-100">{summary?.unique_namespaces ?? 0}</p>
						</div>
						<div class="bg-dark-800/60 rounded-lg p-3 text-center col-span-2 sm:col-span-1">
							<p class="text-xs text-dark-500 uppercase tracking-wider mb-1">CO₂ (operational)</p>
							<p class="text-lg font-semibold text-green-400">
								{formatCO2(summary?.total_co2e_grams ?? 0)}
							</p>
						</div>
						<div class="bg-dark-800/60 rounded-lg p-3 text-center">
							<p class="text-xs text-dark-500 uppercase tracking-wider mb-1">CO₂ (embodied)</p>
							<p class="text-lg font-semibold text-emerald-400">
								{formatCO2(summary?.total_embodied_co2e_grams ?? 0)}
							</p>
						</div>
						<div class="bg-dark-800/60 rounded-lg p-3 text-center">
							<p class="text-xs text-dark-500 uppercase tracking-wider mb-1">Energy</p>
							<p class="text-lg font-semibold text-yellow-400">
								{formatEnergy(summary?.total_energy_joules ?? 0)}
							</p>
						</div>
						<div class="bg-dark-800/60 rounded-lg p-3 text-center col-span-2 sm:col-span-3">
							<p class="text-xs text-dark-500 uppercase tracking-wider mb-1">Total Cost</p>
							<p class="text-xl font-bold text-blue-400">
								{formatCost(summary?.total_cost ?? 0)}
							</p>
						</div>
					</div>
				</DataState>
			</Card>

			<!-- Report configuration recap -->
			<Card title="Configuration Summary">
				<div class="space-y-2 text-sm">
					<div class="flex justify-between items-center py-1.5 border-b border-dark-700/50">
						<span class="text-dark-500">Time range</span>
						<span class="text-dark-200 font-medium">
							{timeRanges.find(r => r.value === last)?.label ?? last}
						</span>
					</div>
					<div class="flex justify-between items-center py-1.5 border-b border-dark-700/50">
						<span class="text-dark-500">Namespace</span>
						<span class="text-dark-200 font-medium">{namespace || 'All namespaces'}</span>
					</div>
					<div class="flex justify-between items-center py-1.5 border-b border-dark-700/50">
						<span class="text-dark-500">Aggregation</span>
						<span class="text-dark-200 font-medium">
							{#if aggregate}
								{granularities.find(g => g.value === granularity)?.label ?? granularity}
							{:else}
								None (raw data)
							{/if}
						</span>
					</div>
					<div class="flex justify-between items-center py-1.5">
						<span class="text-dark-500">Export format</span>
						<span class="text-dark-200 font-medium uppercase">{format}</span>
					</div>
				</div>
			</Card>

			<!-- Download button -->
			<div class="space-y-2">
				<button
					on:click={handleExport}
					disabled={downloading || loading}
					class="w-full flex items-center justify-center gap-2 px-6 py-3 rounded-xl
					       font-semibold text-sm transition-all duration-200
					       {downloading || loading
							? 'bg-dark-700 text-dark-500 cursor-not-allowed'
							: 'bg-green-600 hover:bg-green-500 text-white shadow-lg shadow-green-900/30 hover:shadow-green-800/40'}"
				>
					{#if downloading}
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
						</svg>
						Generating…
					{:else}
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
								d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
						</svg>
						Download {format.toUpperCase()} Report
					{/if}
				</button>

				<!-- Feedback messages -->
				{#if downloadSuccess}
					<div class="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-green-900/30 border border-green-700/40 text-green-400 text-sm">
						<svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
						Report downloaded successfully.
					</div>
				{/if}
				{#if downloadError}
					<div class="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-red-900/30 border border-red-700/40 text-red-400 text-sm">
						<svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
								d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
						</svg>
						{downloadError}
					</div>
				{/if}
			</div>

			<!-- Columns info -->
			<Card title="Exported Columns">
				<div class="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1.5 text-xs text-dark-400 font-mono">
					<span>pod_name</span>
					<span>namespace</span>
					<span>timestamp</span>
					<span>co2e_grams</span>
					<span>embodied_co2e_grams</span>
					<span>joules</span>
					<span>total_cost</span>
					<span>cpu_request</span>
					<span>memory_request</span>
					<span>grid_intensity</span>
					<span>pue</span>
					<span>node</span>
					<span>node_instance_type</span>
					<span>emaps_zone</span>
					<span>is_estimated</span>
					{#if aggregate}
						<span class="text-green-500">period</span>
					{/if}
				</div>
			</Card>

		</div>
	</div>
</div>
