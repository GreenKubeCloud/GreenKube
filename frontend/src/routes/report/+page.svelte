<script>
	import { onMount } from 'svelte';
	import { getNamespaces, getReportSummary, getReportYears, buildReportExportUrl } from '$lib/api.js';
	import {
		reportTimeRanges as timeRanges,
		aggregationLevels,
		groupByOptions,
		buildReportRequestParams
	} from '$lib/reportOptions.js';
	import { formatCO2, formatCost, formatEnergy } from '$lib/utils/format.js';
	import Card from '$lib/components/Card.svelte';
	import DataState from '$lib/components/DataState.svelte';

	// ── Filter state ──────────────────────────────────────────────────────────
	let namespace = '';
	let last = '24h';
	let timeMode = 'relative';
	let selectedYears = [];
	let customStart = toInputDate(new Date(Date.now() - 30 * 24 * 60 * 60 * 1000));
	let customEnd = toInputDate(new Date());
	let aggregationLevel = 'raw';
	let groupBy = 'pod';
	let format = 'csv';

	// ── UI state ──────────────────────────────────────────────────────────────
	let namespaces = [];
	let availableYears = [];
	let yearsLoading = false;
	let yearsError = null;
	let yearsNamespace = null;
	let yearsRequestId = 0;
	let summary = null;
	let loading = false;
	let error = null;
	let downloading = false;
	let downloadError = null;
	let downloadSuccess = false;

	const formats = [
		{ value: 'csv',  label: 'CSV', icon: '📊', description: 'Spreadsheet-compatible, ideal for Excel / Google Sheets' },
		{ value: 'json', label: 'JSON', icon: '🗂️', description: 'Machine-readable, ideal for further processing' }
	];

	function toInputDate(date) {
		return date.toISOString().slice(0, 10);
	}

	function selectRelativeRange(value) {
		timeMode = 'relative';
		last = value;
	}

	function selectYearlyMode() {
		timeMode = 'yearly';
		if (selectedYears.length === 0 && availableYears.length > 0) {
			selectedYears = [availableYears[0]];
		}
	}

	function toggleYear(year) {
		selectedYears = selectedYears.includes(year)
			? selectedYears.filter((selectedYear) => selectedYear !== year)
			: [...selectedYears, year].sort((left, right) => right - left);
	}

	async function refreshAvailableYears(currentNamespace = namespace) {
		const requestId = ++yearsRequestId;
		yearsLoading = true;
		yearsError = null;
		try {
			const years = await getReportYears({ namespace: currentNamespace || undefined });
			if (requestId !== yearsRequestId) return;
			availableYears = years;
			selectedYears = selectedYears.filter((year) => years.includes(year));
			if (timeMode === 'yearly' && selectedYears.length === 0 && years.length > 0) {
				selectedYears = [years[0]];
			}
		} catch (e) {
			if (requestId !== yearsRequestId) return;
			availableYears = [];
			selectedYears = [];
			yearsError = e.message;
		} finally {
			if (requestId === yearsRequestId) {
				yearsLoading = false;
				yearsNamespace = currentNamespace;
			}
		}
	}

	// ── Reactive summary preview ──────────────────────────────────────────────
	$: reportParams = buildReportRequestParams({
		namespace,
		last,
		timeMode,
		years: selectedYears,
		start: customStart,
		end: customEnd,
		aggregationLevel,
		groupBy
	});
	$: aggregate = reportParams.aggregate;
	$: selectionReady = (timeMode !== 'yearly' || selectedYears.length > 0) && (timeMode !== 'custom' || (customStart && customEnd));
	$: previewParams = { namespace, last, timeMode, years: selectedYears.join(','), customStart, customEnd, aggregationLevel, groupBy };
	$: if (previewParams) {
		if (selectionReady) {
			refreshSummary();
		} else {
			summary = null;
			error = null;
			loading = false;
		}
	}
	$: if (namespace !== yearsNamespace) refreshAvailableYears(namespace);
	$: timeRangeLabel = timeMode === 'yearly'
		? (selectedYears.length ? selectedYears.join(', ') : 'No years selected')
		: timeMode === 'custom'
			? `${customStart} to ${customEnd}`
			: (timeRanges.find(r => r.value === last)?.label ?? last);

	async function refreshSummary() {
		loading = true;
		error = null;
		summary = null;
		try {
			summary = await getReportSummary(reportParams);
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	async function handleExport() {
		if (!selectionReady) return;
		downloading = true;
		downloadError = null;
		downloadSuccess = false;
		try {
			const url = buildReportExportUrl({
				...reportParams,
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
								on:click={() => selectRelativeRange(tr.value)}
								class="px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150
								       {timeMode === 'relative' && last === tr.value
										? 'bg-green-600/20 text-green-400 border border-green-600/50'
										: 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:text-dark-200 hover:bg-dark-700'}"
							>
								{tr.label}
							</button>
						{/each}
						<button
							on:click={selectYearlyMode}
							class="px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150
							       {timeMode === 'yearly'
									? 'bg-green-600/20 text-green-400 border border-green-600/50'
									: 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:text-dark-200 hover:bg-dark-700'}"
						>
							Years
						</button>
						<button
							on:click={() => (timeMode = 'custom')}
							class="px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150
							       {timeMode === 'custom'
									? 'bg-green-600/20 text-green-400 border border-green-600/50'
									: 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:text-dark-200 hover:bg-dark-700'}"
						>
							Custom
						</button>
					</div>

					{#if timeMode === 'yearly'}
						<div class="grid grid-cols-3 gap-2">
							{#if yearsLoading}
								<div class="col-span-3 text-xs text-dark-500 px-1 py-2">Loading years…</div>
							{:else if yearsError}
								<div class="col-span-3 text-xs text-red-400 px-1 py-2">{yearsError}</div>
							{:else if availableYears.length === 0}
								<div class="col-span-3 text-xs text-dark-500 px-1 py-2">No years found.</div>
							{:else}
								{#each availableYears as year}
									<button
										on:click={() => toggleYear(year)}
										class="px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150
										       {selectedYears.includes(year)
												? 'bg-green-600/20 text-green-400 border border-green-600/50'
												: 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:text-dark-200 hover:bg-dark-700'}"
									>
										{year}
									</button>
								{/each}
							{/if}
						</div>
					{/if}

					{#if timeMode === 'custom'}
						<div class="grid grid-cols-2 gap-2">
							<label class="text-xs text-dark-500 space-y-1">
								<span>Start</span>
								<input
									type="date"
									bind:value={customStart}
									class="w-full bg-dark-800 border border-dark-600/50 text-dark-200 text-sm rounded-lg px-3 py-2
									       focus:ring-2 focus:ring-green-500/50 focus:border-green-500 transition-colors"
								/>
							</label>
							<label class="text-xs text-dark-500 space-y-1">
								<span>End</span>
								<input
									type="date"
									bind:value={customEnd}
									class="w-full bg-dark-800 border border-dark-600/50 text-dark-200 text-sm rounded-lg px-3 py-2
									       focus:ring-2 focus:ring-green-500/50 focus:border-green-500 transition-colors"
								/>
							</label>
						</div>
					{/if}
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
			<Card title="Aggregation Level">
				<div class="grid grid-cols-1 gap-1.5">
					{#each aggregationLevels as level}
						<button
							on:click={() => (aggregationLevel = level.value)}
							class="px-3 py-1.5 rounded-lg text-sm text-left transition-all duration-150
							       {aggregationLevel === level.value
									? 'bg-green-600/20 text-green-400 border border-green-600/50'
									: 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:text-dark-200 hover:bg-dark-700'}"
						>
							{level.label}
						</button>
					{/each}
				</div>
			</Card>

			<!-- Aggregation grouping -->
			<Card title="Group By">
				<div class="grid grid-cols-2 gap-2">
					{#each groupByOptions as option}
						<button
							on:click={() => (groupBy = option.value)}
							class="px-3 py-2 rounded-lg text-sm font-medium transition-all duration-150
							       {groupBy === option.value
									? 'bg-green-600/20 text-green-400 border border-green-600/50'
									: 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:text-dark-200 hover:bg-dark-700'}"
						>
							{option.label}
						</button>
					{/each}
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
							<p class="text-xs text-dark-500 uppercase tracking-wider mb-1">Scope 2 CO₂ (electricity)</p>
							<p class="text-lg font-semibold text-green-400">
								{formatCO2(summary?.total_co2e_grams ?? 0)}
							</p>
						</div>
						<div class="bg-dark-800/60 rounded-lg p-3 text-center">
							<p class="text-xs text-dark-500 uppercase tracking-wider mb-1">Scope 3 CO₂ (hardware)</p>
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
						<span class="text-dark-200 font-medium">{timeRangeLabel}</span>
					</div>
					<div class="flex justify-between items-center py-1.5 border-b border-dark-700/50">
						<span class="text-dark-500">Namespace</span>
						<span class="text-dark-200 font-medium">{namespace || 'All namespaces'}</span>
					</div>
					<div class="flex justify-between items-center py-1.5 border-b border-dark-700/50">
						<span class="text-dark-500">Aggregation level</span>
						<span class="text-dark-200 font-medium">
							{aggregationLevels.find(level => level.value === aggregationLevel)?.label ?? aggregationLevel}
						</span>
					</div>
					{#if aggregate}
						<div class="flex justify-between items-center py-1.5 border-b border-dark-700/50">
							<span class="text-dark-500">Group by</span>
							<span class="text-dark-200 font-medium">
								{groupByOptions.find(option => option.value === groupBy)?.label ?? groupBy}
							</span>
						</div>
					{/if}
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
					disabled={downloading || loading || !selectionReady}
					class="w-full flex items-center justify-center gap-2 px-6 py-3 rounded-xl
					       font-semibold text-sm transition-all duration-200
					       {downloading || loading || !selectionReady
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
					<span>co2e_grams (Scope 2)</span>
					<span>embodied_co2e_grams (Scope 3)</span>
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
