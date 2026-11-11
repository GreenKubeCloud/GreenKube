<script>
	import { onDestroy, onMount } from 'svelte';
	import { getSourceTrackRecord, listSourceRuns, listSources } from '$lib/api.js';
	import { formatDate, formatRelativeTime } from '$lib/utils/format.js';
	import { runTone, sortSources, sourceStatus, trackRecordFor } from '$lib/sources.js';
	import StatusPill from '$lib/components/StatusPill.svelte';

	const RUN_LIMIT = 20;

	let sources = [];
	let problems = [];
	let trackEntries = [];
	let loading = true;
	let error = '';
	let expanded = null;
	let runs = {};
	let runsLoading = {};
	let runsError = {};
	let controller = new AbortController();

	onMount(load);
	onDestroy(() => controller.abort());

	async function load() {
		controller.abort();
		controller = new AbortController();
		const { signal } = controller;
		loading = true;
		error = '';
		runs = {};
		runsError = {};
		const [list, track] = await Promise.allSettled([listSources({ signal }), getSourceTrackRecord({ signal })]);
		if (signal.aborted) return;
		if (list.status === 'fulfilled') {
			sources = sortSources(Array.isArray(list.value?.sources) ? list.value.sources : []);
			problems = Array.isArray(list.value?.problems) ? list.value.problems : [];
		} else {
			error = list.reason?.message || 'Recommendation sources are unavailable.';
		}
		trackEntries = track.status === 'fulfilled' && Array.isArray(track.value?.entries) ? track.value.entries : [];
		loading = false;
		if (expanded) loadRuns(expanded);
	}

	async function loadRuns(id) {
		runsLoading = { ...runsLoading, [id]: true };
		runsError = { ...runsError, [id]: '' };
		try {
			const result = await listSourceRuns(id, { limit: RUN_LIMIT, signal: controller.signal });
			runs = { ...runs, [id]: Array.isArray(result) ? result : [] };
		} catch (e) {
			if (e?.name !== 'AbortError') runsError = { ...runsError, [id]: e?.message || 'Runs are unavailable.' };
		} finally {
			runsLoading = { ...runsLoading, [id]: false };
		}
	}

	function toggle(id) {
		expanded = expanded === id ? null : id;
		if (expanded && !runs[expanded]) loadRuns(expanded);
	}
</script>

<svelte:head><title>Sources · GreenKube</title></svelte:head>

<div class="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1200px] mx-auto">
	<header class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Recommendation sources</h1>
			<p class="text-sm text-dark-500 mt-1">
				Every analyzer and connector GreenKube aggregates, with its health, last run and measured track record
			</p>
		</div>
		<button type="button" class="btn-secondary text-sm" on:click={load} disabled={loading}>
			{loading ? 'Loading…' : 'Reload'}
		</button>
	</header>

	{#if problems.length}
		<section class="card border-red-700/40 space-y-2" aria-labelledby="problems-heading" data-testid="source-problems">
			<h2 id="problems-heading" class="text-sm font-semibold text-red-300">Registry problems</h2>
			<ul class="space-y-1 text-xs">
				{#each problems as problem, i (i)}
					<li class="text-dark-200">
						<span class="font-mono text-dark-400">{problem.code}</span>
						{#if problem.source_id}· {problem.source_id}{/if} · {problem.message}
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if loading && !sources.length}
		<p class="text-sm text-dark-400" role="status">Loading sources…</p>
	{:else if error}
		<div class="card text-sm text-amber-300" role="alert">{error}</div>
	{:else if !sources.length}
		<div class="card text-sm text-dark-400" data-testid="sources-empty">No recommendation source is registered.</div>
	{:else}
		<ul class="space-y-3" data-testid="sources-list">
			{#each sources as source (source.id)}
				{@const info = sourceStatus(source.status)}
				{@const record = trackRecordFor(source, trackEntries)}
				<li class="card-compact space-y-3" data-testid="source-{source.id}">
					<div class="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
						<div class="min-w-0">
							<h2 class="text-sm font-semibold text-dark-100">
								{source.display_name || source.id}
								<span class="ml-1 text-xs font-normal text-dark-500">{source.id}</span>
							</h2>
							{#if source.description}<p class="text-xs text-dark-400">{source.description}</p>{/if}
							<p class="text-[11px] text-dark-500 mt-1">
								{source.origin}{#if source.tier} · {source.tier}{/if}{#if source.version} · v{source.version}{/if}
								{#if source.enabled} · enabled: {source.enabled}{/if}
								{#if source.capabilities?.length} · {source.capabilities.join(', ')}{/if}
							</p>
						</div>
						<StatusPill label={info.label} tone={info.tone} icon={info.icon} />
					</div>
					{#if source.status_reason}<p class="text-xs text-dark-300">{source.status_reason}</p>{/if}
					<dl class="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
						<div><dt class="text-dark-500">Last run</dt><dd class="text-dark-200">{source.last_run ? formatRelativeTime(source.last_run.finished_at) : 'Never'}</dd></div>
						<div><dt class="text-dark-500">Last success</dt><dd class="text-dark-200">{source.last_success_at ? formatRelativeTime(source.last_success_at) : '—'}</dd></div>
						<div><dt class="text-dark-500">Signals</dt><dd class="text-dark-200">{source.signal_count ?? 0}</dd></div>
						<div><dt class="text-dark-500">Recommendations</dt><dd class="text-dark-200">{source.recommendation_count ?? 0}</dd></div>
					</dl>
					{#if record.length}
						<p class="text-xs text-dark-400" data-testid="track-record">
							Track record:
							{#each record as entry, i (entry.capability)}
								<span class="text-dark-200">{entry.capability}: {entry.summary || `${entry.decided} decided`}</span>{#if i < record.length - 1}<span> · </span>{/if}
							{/each}
						</p>
					{/if}
					<button
						type="button"
						class="text-xs text-green-400 hover:text-green-300"
						aria-expanded={expanded === source.id}
						aria-controls="runs-{source.id}"
						on:click={() => toggle(source.id)}
					>
						{expanded === source.id ? 'Hide runs' : 'Show recent runs'}
					</button>
					{#if expanded === source.id}
						<div id="runs-{source.id}" class="overflow-x-auto">
							{#if runsLoading[source.id]}
								<p class="text-xs text-dark-400" role="status">Loading runs…</p>
							{:else if runsError[source.id]}
								<p class="text-xs text-amber-300" role="alert">{runsError[source.id]}</p>
							{:else if !runs[source.id]?.length}
								<p class="text-xs text-dark-400">No run recorded yet.</p>
							{:else}
								<table class="w-full min-w-[520px] text-xs" data-testid="runs-{source.id}">
									<caption class="sr-only">Recent runs of {source.display_name || source.id}</caption>
									<thead>
										<tr class="text-left text-dark-500">
											<th scope="col" class="py-1 font-medium">Finished</th>
											<th scope="col" class="py-1 font-medium">Status</th>
											<th scope="col" class="py-1 font-medium">Duration</th>
											<th scope="col" class="py-1 font-medium">Signals</th>
											<th scope="col" class="py-1 font-medium">Details</th>
										</tr>
									</thead>
									<tbody>
										{#each runs[source.id] as run, i (run.run_id ?? i)}
											<tr class="border-t border-dark-700/50 align-top">
												<td class="py-1 text-dark-300"><time datetime={run.finished_at}>{formatDate(run.finished_at)}</time></td>
												<td class="py-1"><StatusPill label={run.status} tone={runTone(run.status)} /></td>
												<td class="py-1 text-dark-300">{typeof run.duration_seconds === 'number' ? `${run.duration_seconds.toFixed(1)} s` : '—'}</td>
												<td class="py-1 text-dark-300">{run.signal_count ?? 0}</td>
												<td class="py-1 text-dark-400">
													{#if run.error_code}<span class="font-mono">{run.error_code}</span>{/if}
													{run.error ?? run.health_reason ?? ''}
													{#each run.diagnostics ?? [] as diag, d (d)}
														<span class="block">{diag.level}: {diag.message}</span>
													{/each}
												</td>
											</tr>
										{/each}
									</tbody>
								</table>
							{/if}
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</div>
