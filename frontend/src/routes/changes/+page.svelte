<script>
	/**
	 * Changes board (K4): every change GreenKube delivered, from approved plan
	 * to measured outcome, in one pipeline. Replaces the pull-request lists of
	 * the Automation page; reads only.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { page } from '$app/stores';
	import {
		BOARD_STAGES,
		boardFilterQuery,
		boardNamespaces,
		buildChangeBoard,
		filterBoardRows,
		groupByStage,
		loadChangeBoard,
		loadMoreChanges,
		parseBoardFilters
	} from '$lib/changes.js';
	import ChangeRow from './ChangeRow.svelte';

	let data = null;
	let loading = true;
	let loadingMore = false;
	let moreError = '';
	let controller = new AbortController();

	const initial = parseBoardFilters($page?.url?.searchParams);
	let stage = initial.stage;
	let namespace = initial.namespace;
	let attention = initial.attention;
	let query = initial.query;

	onMount(load);
	onDestroy(() => controller.abort());

	async function load() {
		controller.abort();
		controller = new AbortController();
		loading = true;
		moreError = '';
		const result = await loadChangeBoard({ signal: controller.signal });
		if (!result) return;
		data = result;
		loading = false;
	}

	async function loadOlder() {
		if (!data?.nextCursor || loadingMore) return;
		loadingMore = true;
		moreError = '';
		try {
			const more = await loadMoreChanges(data.nextCursor, { signal: controller.signal });
			data = { ...data, changes: [...data.changes, ...more.changes], nextCursor: more.nextCursor };
		} catch (error) {
			if (error?.name !== 'AbortError') moreError = error?.message || 'Older changes could not be loaded.';
		} finally {
			loadingMore = false;
		}
	}

	function clearFilters() {
		stage = '';
		namespace = '';
		attention = false;
		query = '';
	}

	function syncUrl(search) {
		// Keep the filters shareable without a navigation (no reload of the board).
		if (typeof window === 'undefined' || !window.history?.replaceState) return;
		const next = `${window.location.pathname}${search}`;
		if (`${window.location.pathname}${window.location.search}` !== next) {
			try {
				window.history.replaceState(window.history.state, '', next);
			} catch {
				// A sandboxed frame may refuse; the filters still apply.
			}
		}
	}

	$: rows = data ? buildChangeBoard(data) : [];
	$: namespaces = boardNamespaces(rows);
	$: filters = { stage, namespace, attention, query };
	$: visible = filterBoardRows(rows, filters);
	$: columns = groupByStage(visible).filter((column) => !stage || column.id === stage);
	$: attentionCount = rows.filter((row) => row.attention).length;
	$: filtered = Boolean(stage || namespace || attention || query);
	// Several reads often fail with the same message (API down, 502, 401); keep one alert each.
	$: errors = data ? [...new Set(Object.values(data.errors).filter(Boolean))] : [];
	$: if (!loading) syncUrl(boardFilterQuery(filters));
</script>

<svelte:head><title>Changes · GreenKube</title></svelte:head>

<div class="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1800px] mx-auto">
	<header class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div class="min-w-0">
			<h1 class="text-2xl font-bold text-dark-100">Changes</h1>
			<p class="text-sm text-dark-500 mt-1">
				Every change in flight, from approved plan to pull request, merge, sync, rollout and measured outcome
			</p>
		</div>
		<button type="button" class="btn-secondary text-sm shrink-0 self-start" on:click={load} disabled={loading}>
			{loading ? 'Loading…' : 'Reload'}
		</button>
	</header>

	<form class="card-compact grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5 lg:items-end" role="search" aria-label="Filter changes" on:submit|preventDefault={() => {}}>
		<label class="flex flex-col gap-1 text-xs text-dark-400">
			Stage
			<select bind:value={stage} class="rounded-lg border border-dark-600/50 bg-dark-800 px-2 py-1.5 text-sm text-dark-100" data-testid="filter-stage">
				<option value="">All stages</option>
				{#each BOARD_STAGES as item (item.id)}<option value={item.id}>{item.label}</option>{/each}
			</select>
		</label>
		<label class="flex flex-col gap-1 text-xs text-dark-400">
			Namespace
			<select bind:value={namespace} class="rounded-lg border border-dark-600/50 bg-dark-800 px-2 py-1.5 text-sm text-dark-100" data-testid="filter-namespace">
				<option value="">All namespaces</option>
				{#each namespaces as name (name)}<option value={name}>{name}</option>{/each}
				{#if namespace && !namespaces.includes(namespace)}<option value={namespace}>{namespace}</option>{/if}
			</select>
		</label>
		<label class="flex flex-col gap-1 text-xs text-dark-400 lg:col-span-2">
			Search
			<input
				type="search"
				bind:value={query}
				placeholder="Workload, repository, plan or #recommendation"
				class="rounded-lg border border-dark-600/50 bg-dark-800 px-2 py-1.5 text-sm text-dark-100 placeholder:text-dark-500"
				data-testid="filter-query"
			/>
		</label>
		<div class="flex flex-wrap items-center gap-3">
			<label class="flex items-center gap-2 text-xs text-dark-300">
				<input type="checkbox" bind:checked={attention} class="accent-green-500" data-testid="filter-attention" />
				Needs attention ({attentionCount})
			</label>
			{#if filtered}
				<button type="button" class="text-xs text-green-400 hover:text-green-300" on:click={clearFilters}>Clear filters</button>
			{/if}
		</div>
	</form>

	{#each errors as message (message)}
		<p class="card-compact text-sm text-amber-300" role="alert">{message}</p>
	{/each}
	{#if data?.skippedLookups}
		<p class="text-xs text-dark-400" role="status" data-testid="skipped-lookups">
			{data.skippedLookups} more pull requests or plans were not read; their rows may show an earlier stage. Reload to refresh.
		</p>
	{/if}

	{#if loading && !data}
		<p class="text-sm text-dark-400" role="status">Loading changes…</p>
	{:else if !rows.length && !errors.length}
		<div class="card space-y-2" data-testid="board-empty">
			<h2 class="text-lg font-semibold text-dark-100">No change in flight</h2>
			<p class="text-sm text-dark-400">
				Open a pull request from a recommendation and follow it here: plan, pull request, merge, sync, rollout and
				measurement.
			</p>
			<a href="/recommendations" class="text-sm text-green-400 hover:text-green-300">Open the recommendations</a>
		</div>
	{:else}
		<p class="sr-only" role="status" aria-live="polite">{visible.length} of {rows.length} changes shown</p>
		{#if filtered && !visible.length}
			<p class="text-sm text-dark-400" data-testid="board-no-match">No change matches these filters.</p>
		{/if}
		<div
			class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-7"
			data-testid="change-board"
		>
			{#each columns as column (column.id)}
				<section
					class="rounded-xl border border-dark-700/50 bg-dark-900 p-3 space-y-3 min-w-0"
					aria-labelledby="stage-{column.id}"
					data-testid="stage-{column.id}"
				>
					<div>
						<h2 id="stage-{column.id}" class="flex items-center justify-between gap-2 text-sm font-semibold text-dark-200">
							<span>{column.label}</span>
							<span class="rounded-full bg-dark-800 px-2 py-0.5 text-xs font-medium text-dark-300" data-testid="count-{column.id}">
								{column.rows.length}<span class="sr-only">{column.rows.length === 1 ? ' change' : ' changes'}</span>
							</span>
						</h2>
						<p class="text-[11px] text-dark-500 mt-0.5">{column.description}</p>
					</div>
					{#if column.rows.length}
						<ul class="space-y-2">
							{#each column.rows as row (row.key)}
								<li><ChangeRow {row} /></li>
							{/each}
						</ul>
					{:else}
						<p class="text-xs text-dark-500">None</p>
					{/if}
				</section>
			{/each}
		</div>
		{#if data?.nextCursor}
			<div class="flex flex-col items-start gap-1">
				<button type="button" class="btn-secondary text-sm" on:click={loadOlder} disabled={loadingMore} data-testid="load-older">
					{loadingMore ? 'Loading…' : 'Load older changes'}
				</button>
				{#if moreError}<p class="text-xs text-amber-300" role="alert">{moreError}</p>{/if}
			</div>
		{/if}
	{/if}
</div>
