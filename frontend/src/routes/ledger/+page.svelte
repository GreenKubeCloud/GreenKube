<script>
	import { onDestroy } from 'svelte';
	import { getChangeMeasurements, getRecommendationSavings, listChanges } from '#lib/api.js';
	import { formatCO2, formatDate } from '#lib/utils/format.js';
	import { formatMoney } from '#lib/utils/decision.js';
	import { CHANGE_STATES, changeTarget, latestMeasurement, resolveChange, stateLabel, stateTone } from '#lib/changes.js';
	import { safeHttpUrl } from '#lib/recommendationDetail.js';
	import StatusPill from '#lib/components/StatusPill.svelte';
	import MeasurementTable from '#lib/components/MeasurementTable.svelte';
	import { SAVINGS_TERMS, realizationText, savingsFigures } from '#lib/savingsVocabulary.js';

	/** @type {{change: string|null}} */
	export let data;

	const PAGE_SIZE = 50;

	let savings = null;
	let savingsError = '';
	let changes = [];
	let nextCursor = null;
	let listError = '';
	let loadingList = false;
	let stateFilter = '';
	let selected = null;
	let selectedMeasurements = [];
	let selectedError = '';
	let selectedMissing = false;
	let loadingSelected = false;
	let controller = new AbortController();
	let loadedKey = null;

	onDestroy(() => controller.abort());

	$: key = `${data?.change ?? ''}|${stateFilter}`;
	$: if (key !== loadedKey) load();

	async function load() {
		loadedKey = key;
		controller.abort();
		controller = new AbortController();
		const { signal } = controller;
		loadSavings(signal);
		if (data?.change) {
			changes = [];
			nextCursor = null;
			await loadSelected(data.change, signal);
		} else {
			selected = null;
			await loadPage(signal, null);
		}
	}

	async function loadSavings(signal) {
		try {
			const result = await getRecommendationSavings({ signal });
			if (!signal.aborted) {
				savings = result;
				savingsError = '';
			}
		} catch (error) {
			if (!signal.aborted) savingsError = error?.message || 'Savings are temporarily unavailable.';
		}
	}

	async function loadPage(signal, cursor) {
		loadingList = true;
		listError = '';
		try {
			const result = await listChanges({ state: stateFilter ? [stateFilter] : undefined, limit: PAGE_SIZE, cursor, signal });
			if (signal.aborted) return;
			const items = Array.isArray(result?.items) ? result.items : [];
			changes = cursor ? [...changes, ...items] : items;
			nextCursor = result?.next_cursor ?? null;
		} catch (error) {
			if (!signal.aborted) listError = error?.message || 'Changes are temporarily unavailable.';
		} finally {
			if (!signal.aborted) loadingList = false;
		}
	}

	async function loadSelected(id, signal) {
		loadingSelected = true;
		selected = null;
		selectedMeasurements = [];
		selectedError = '';
		selectedMissing = false;
		try {
			const change = await resolveChange(id, { signal });
			if (signal.aborted) return;
			if (!change) {
				selectedMissing = true;
				return;
			}
			selected = change;
			const result = await getChangeMeasurements(change.change_id, { signal });
			if (!signal.aborted) selectedMeasurements = Array.isArray(result?.items) ? result.items : [];
		} catch (error) {
			if (!signal.aborted) selectedError = error?.message || 'The change could not be loaded.';
		} finally {
			if (!signal.aborted) loadingSelected = false;
		}
	}

	function loadMore() {
		if (nextCursor && !loadingList) loadPage(controller.signal, nextCursor);
	}

	function money(value) {
		return formatMoney(value ?? 0, savings?.currency);
	}

	$: blocks = savings
		? [
				{ id: 'realized', title: SAVINGS_TERMS.measuredAllocatedToDate, pill: 'Measured', tone: 'green', block: figures.measuredAllocatedToDate, unit: '', note: 'What the changed workloads stopped being charged, accrued in the ledger.' },
				{ id: 'run-rate', title: SAVINGS_TERMS.measuredAllocatedRunRate, pill: 'Measured', tone: 'green', block: figures.measuredAllocatedAnnual, unit: '/yr', note: 'Current yearly allocated rate of verified changes.' },
				...(figures.realizedBilledToDate
					? [{ id: 'billed', title: SAVINGS_TERMS.realizedBilled, pill: 'Realized', tone: 'green', block: figures.realizedBilledToDate, unit: '', note: 'Savings seen on the bill (capacity removed).' }]
					: []),
				{ id: 'pending', title: SAVINGS_TERMS.pendingVerification, pill: 'Projected', tone: 'blue', block: figures.projectedAnnual, unit: '/yr', note: 'Projection for applied changes not measured yet.' },
				{ id: 'declared', title: SAVINGS_TERMS.userDeclared, pill: 'Declared', tone: 'dark', block: figures.userDeclaredAnnual, unit: '/yr', note: 'Declared on manual applies; never counted as measured.' }
			]
		: [];
	$: figures = savingsFigures(savings);
	$: realization = realizationText(figures.realizationFactor);
	$: current = latestMeasurement(selectedMeasurements);
	$: history = selectedMeasurements.filter((item) => item !== current).reverse();
</script>

<svelte:head><title>Savings ledger · GreenKube</title></svelte:head>

<div class="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1400px] mx-auto">
	<header>
		<h1 class="text-2xl font-bold text-dark-100">Savings ledger</h1>
		<p class="text-sm text-dark-500 mt-1">Applied changes, their measurements and the allocated savings measured on them</p>
	</header>

	<!-- Totals per method, never summed -->
	<section aria-labelledby="totals-heading" class="space-y-2">
		<h2 id="totals-heading" class="sr-only">Totals by method</h2>
		{#if savingsError}
			<p class="card text-sm text-amber-300" role="alert">{savingsError}</p>
		{:else}
			<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
				{#each blocks as item (item.id)}
					<article class="card-compact space-y-1" data-testid="ledger-total-{item.id}">
						<div class="flex items-center justify-between gap-2">
							<h3 class="stat-label text-xs">{item.title}</h3>
							<StatusPill label={item.pill} tone={item.tone} />
						</div>
						<p class="text-xl font-bold text-dark-100">{money(item.block?.cost)}{item.unit}</p>
						<p class="text-sm text-dark-300">{formatCO2(item.block?.co2e_grams ?? 0)} CO₂e{item.unit}</p>
						<p class="text-[11px] text-dark-500">{item.note}</p>
					</article>
				{/each}
			</div>
			<p class="text-xs text-dark-500">Each figure is a different kind of saving; they are never added together.</p>
			{#if realization}
				<p class="text-xs text-dark-400" data-testid="ledger-realization-factor">{realization}</p>
			{/if}
		{/if}
	</section>

	{#if data?.change}
		<!-- One change (PR deep link) -->
		<section class="card space-y-4" aria-labelledby="selected-heading" data-testid="selected-change">
			<div class="flex flex-wrap items-center justify-between gap-2">
				<h2 id="selected-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">
					Change <code class="normal-case break-all">{data.change}</code>
				</h2>
				<a href="/ledger" class="text-xs text-green-400 hover:text-green-300">Show all changes</a>
			</div>
			{#if loadingSelected}
				<p class="text-sm text-dark-400" role="status">Loading the change…</p>
			{:else if selectedError}
				<p class="text-sm text-amber-300" role="alert">{selectedError}</p>
			{:else if selectedMissing}
				<p class="text-sm text-dark-400" data-testid="selected-missing">
					No applied change matches this id yet. A change appears here once its pull request is merged, synced and rolled out.
				</p>
			{:else if selected}
				<div class="flex flex-wrap items-center gap-2 text-sm">
					<span class="text-dark-100">{changeTarget(selected)}</span>
					<StatusPill label={stateLabel(selected.state)} tone={stateTone(selected.state)} />
					{#if selected.plan_id}
						<a href="/changes/{encodeURIComponent(selected.plan_id)}" class="text-xs text-green-400 hover:text-green-300">Plan and pull request →</a>
					{/if}
					{#if safeHttpUrl(selected.pr_url)}
						<a href={safeHttpUrl(selected.pr_url)} target="_blank" rel="noopener noreferrer" class="text-xs text-green-400 hover:text-green-300">Pull request ↗</a>
					{/if}
				</div>
				{#if current}
					<MeasurementTable measurement={current} />
					{#if current.recommendations?.length}
						<div>
							<h3 class="text-xs font-semibold text-dark-400 mb-1">Split between recommendations</h3>
							<ul class="text-xs space-y-1">
								{#each current.recommendations as row (row.recommendation_id)}
									<li>
										<a href="/recommendations/{row.recommendation_id}" class="text-green-400 hover:text-green-300">#{row.recommendation_id}</a>
										<span class="text-dark-400">· {String(row.outcome).replaceAll('_', ' ')}</span>
									</li>
								{/each}
							</ul>
						</div>
					{/if}
					{#if history.length}
						<details class="text-xs">
							<summary class="cursor-pointer text-dark-400 hover:text-dark-200">Earlier measurements ({history.length})</summary>
							<div class="mt-2 space-y-4">
								{#each history as item (item.measurement_id)}<MeasurementTable measurement={item} />{/each}
							</div>
						</details>
					{/if}
				{:else}
					<p class="text-sm text-dark-400">No measurement decided yet ({stateLabel(selected.state).toLowerCase()}).</p>
				{/if}
			{/if}
		</section>
	{:else}
		<!-- Changes -->
		<section class="card space-y-3" aria-labelledby="changes-heading">
			<div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
				<h2 id="changes-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Applied changes</h2>
				<div class="flex items-center gap-2">
					<label for="state-filter" class="text-xs text-dark-400">State</label>
					<select id="state-filter" bind:value={stateFilter} class="bg-dark-800 border border-dark-600/50 text-dark-200 text-sm rounded-lg px-2 py-1">
						<option value="">All</option>
						{#each Object.entries(CHANGE_STATES) as [value, info] (value)}<option {value}>{info.label}</option>{/each}
					</select>
				</div>
			</div>
			{#if listError}
				<p class="text-sm text-amber-300" role="alert">{listError}</p>
			{:else if loadingList && !changes.length}
				<p class="text-sm text-dark-400" role="status">Loading changes…</p>
			{:else if !changes.length}
				<p class="text-sm text-dark-400" data-testid="changes-empty">
					No applied change yet. Changes are recorded when a merged pull request rolls out, or when GreenKube detects a
					manual change to a recommended workload.
				</p>
			{:else}
				<ul class="divide-y divide-dark-700/50" data-testid="changes-list">
					{#each changes as item (item.change_id)}
						<li class="py-2 flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
							<div class="min-w-0">
								<a href="/ledger?change={encodeURIComponent(item.change_id)}" class="text-sm text-dark-100 hover:text-green-300 break-all">{changeTarget(item)}</a>
								<p class="text-xs text-dark-500">
									Rolled out {formatDate(item.rollout_completed_at)} · {item.application_method ?? 'detected'}
									{#if item.plan_id}· <a href="/changes/{encodeURIComponent(item.plan_id)}" class="hover:text-dark-200">plan</a>{/if}
								</p>
							</div>
							<StatusPill label={stateLabel(item.state)} tone={stateTone(item.state)} />
						</li>
					{/each}
				</ul>
				{#if nextCursor}
					<button type="button" class="btn-secondary text-xs" on:click={loadMore} disabled={loadingList}>
						{loadingList ? 'Loading…' : 'Load more'}
					</button>
				{/if}
			{/if}
		</section>
	{/if}
</div>
