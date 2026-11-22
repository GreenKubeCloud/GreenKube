<script>
	import { onDestroy } from 'svelte';
	import { ApiError, getAutomationPlan, getChangeMeasurements, getRecommendationPullRequests } from '#lib/api.js';
	import { formatDate } from '#lib/utils/format.js';
	import { changeTarget, formatUntrustedHours, latestMeasurement, resolveChange, stateLabel, stateTone } from '#lib/changes.js';
	import { safeHttpUrl } from '#lib/recommendationDetail.js';
	import StatusPill from '#lib/components/StatusPill.svelte';
	import MeasurementTable from '#lib/components/MeasurementTable.svelte';
	import ReleasedCapacity from '#lib/components/ReleasedCapacity.svelte';
	import { demoMode } from '#lib/stores.js';

	/** @type {{planId: string}} */
	export let data;

	let plan = null;
	let planMissing = false;
	let planError = '';
	let change = null;
	let changeError = '';
	let measurements = [];
	let pullRequest = null;
	let loading = true;
	let controller = null;
	let loadedId = null;

	onDestroy(() => controller?.abort());

	$: if (data?.planId !== loadedId) load(data?.planId);

	async function load(planId) {
		loadedId = planId;
		controller?.abort();
		controller = new AbortController();
		const { signal } = controller;
		loading = true;
		plan = null;
		planMissing = false;
		planError = '';
		change = null;
		changeError = '';
		measurements = [];
		pullRequest = null;

		const [planResult, changeResult] = await Promise.allSettled([
			getAutomationPlan(planId),
			resolveChange(planId, { signal, preferPlan: true })
		]);
		if (signal.aborted) return;
		if (planResult.status === 'fulfilled') {
			plan = planResult.value;
		} else if (planResult.reason instanceof ApiError && planResult.reason.status === 404) {
			planMissing = true;
		} else {
			planError = planResult.reason?.message || 'The plan could not be loaded.';
		}
		if (changeResult.status === 'fulfilled') change = changeResult.value;
		else if (changeResult.reason?.name !== 'AbortError') changeError = 'Changes are temporarily unavailable.';

		const recommendationId = plan?.recommendation_ids?.[0] ?? change?.recommendation_ids?.[0];
		const [measurementResult, prResult] = await Promise.allSettled([
			change ? getChangeMeasurements(change.change_id, { signal }) : Promise.resolve(null),
			recommendationId != null ? getRecommendationPullRequests(recommendationId, { signal }) : Promise.resolve([])
		]);
		if (signal.aborted) return;
		measurements = measurementResult.status === 'fulfilled' && Array.isArray(measurementResult.value?.items) ? measurementResult.value.items : [];
		const prs = prResult.status === 'fulfilled' && Array.isArray(prResult.value) ? prResult.value : [];
		pullRequest = prs.find((pr) => pr.plan_id === planId) ?? null;
		loading = false;
	}

	$: recommendationIds = plan?.recommendation_ids ?? change?.recommendation_ids ?? [];
	$: current = latestMeasurement(measurements);
	$: prUrl = safeHttpUrl(pullRequest?.pr_url ?? change?.pr_url);
	$: ledgerHref = `/ledger?change=${encodeURIComponent(change?.change_id ?? data?.planId ?? '')}`;
</script>

<svelte:head><title>Change {data?.planId} · GreenKube</title></svelte:head>

<div class="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1200px] mx-auto">
	<nav aria-label="Breadcrumb" class="text-xs text-dark-500">
		<a href="/changes" class="hover:text-dark-200">Changes</a>
		<span aria-hidden="true"> / </span>
		<span aria-current="page" class="break-all">{data?.planId}</span>
	</nav>

	<header>
		<h1 class="text-2xl font-bold text-dark-100">Change</h1>
		<p class="text-sm text-dark-500 mt-1 break-all">Plan <code>{data?.planId}</code></p>
	</header>

	{#if loading}
		<p class="text-sm text-dark-400" role="status">Loading the change…</p>
	{:else if planMissing && !change}
		<div class="card space-y-2" data-testid="not-found">
			<h2 class="text-lg font-semibold text-dark-100">Change not found</h2>
			<p class="text-sm text-dark-400">
				No plan or applied change has this id. Plans that never opened a pull request expire and are removed by retention.
			</p>
			<a href="/ledger" class="text-sm text-green-400 hover:text-green-300">Open the ledger</a>
		</div>
	{:else}
		<div class="grid grid-cols-1 gap-6 lg:grid-cols-2">
			<!-- Plan -->
			<section class="card space-y-3" aria-labelledby="plan-heading" data-testid="plan">
				<h2 id="plan-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Plan</h2>
				{#if planError && $demoMode}
					<!-- The demo records changes without a GitOps plan service: say so once, not as an error. -->
					<p class="text-xs text-dark-400" data-testid="plan-demo">Demo data: the plan and its diff are not stored in the demo.</p>
				{:else if planError}
					<p class="text-xs text-amber-300" role="alert">{planError}</p>
				{:else if plan}
					<dl class="grid grid-cols-2 gap-2 text-xs">
						<div><dt class="text-dark-500">Status</dt><dd class="text-dark-100">{plan.status}</dd></div>
						<div><dt class="text-dark-500">Operation</dt><dd class="text-dark-200">{plan.operation_status ?? '—'}</dd></div>
						<div class="col-span-2"><dt class="text-dark-500">Repository</dt><dd class="text-dark-200 break-all">{plan.source?.repo ?? '—'} ({plan.source?.base_branch ?? '—'})</dd></div>
						<div class="col-span-2"><dt class="text-dark-500">Path</dt><dd class="text-dark-200 break-all">{plan.source?.path ?? '—'}</dd></div>
						<div><dt class="text-dark-500">Created</dt><dd class="text-dark-200">{formatDate(plan.created_at)}</dd></div>
						<div><dt class="text-dark-500">Controller</dt><dd class="text-dark-200">{plan.source?.controller ?? '—'}</dd></div>
					</dl>
					{#if plan.files?.length}
						<ul class="text-xs text-dark-300 font-mono space-y-0.5" aria-label="Files">
							{#each plan.files as file (file.path)}<li class="break-all">{file.operation} {file.path}</li>{/each}
						</ul>
					{/if}
				{:else}
					<p class="text-sm text-dark-400">The plan is no longer stored (retention); the applied change below is kept.</p>
				{/if}
				{#if recommendationIds.length}
					<p class="text-xs text-dark-400">
						Recommendations:
						{#each recommendationIds as id, i (id)}
							<a href="/recommendations/{id}" class="text-green-400 hover:text-green-300">#{id}</a>{#if i < recommendationIds.length - 1}, {/if}
						{/each}
					</p>
				{/if}
			</section>

			<!-- Pull request -->
			<section class="card space-y-3" aria-labelledby="pr-heading" data-testid="pull-request">
				<h2 id="pr-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Pull request</h2>
				{#if pullRequest || prUrl}
					<dl class="grid grid-cols-2 gap-2 text-xs">
						<div><dt class="text-dark-500">Status</dt><dd class="text-dark-100">{pullRequest?.status ?? '—'}</dd></div>
						<div><dt class="text-dark-500">Sync</dt><dd class="text-dark-200">{pullRequest?.sync_status ?? '—'}</dd></div>
						{#if pullRequest?.merged_at}<div><dt class="text-dark-500">Merged</dt><dd class="text-dark-200">{formatDate(pullRequest.merged_at)}</dd></div>{/if}
						{#if pullRequest?.sync_observed_at}<div><dt class="text-dark-500">Synced</dt><dd class="text-dark-200">{formatDate(pullRequest.sync_observed_at)}</dd></div>{/if}
					</dl>
					{#if prUrl}
						<a href={prUrl} target="_blank" rel="noopener noreferrer" class="text-sm text-green-400 hover:text-green-300">
							Open the pull request{pullRequest?.pr_number ? ` #${pullRequest.pr_number}` : ''} ↗
						</a>
					{/if}
				{:else}
					<p class="text-sm text-dark-400">No pull request is recorded for this plan.</p>
				{/if}
			</section>
		</div>

		<!-- Applied change and measurement -->
		<section class="card space-y-3" aria-labelledby="change-heading" data-testid="change">
			<div class="flex flex-wrap items-center justify-between gap-2">
				<h2 id="change-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Applied change and measurement</h2>
				{#if change}<StatusPill label={stateLabel(change.state)} tone={stateTone(change.state)} />{/if}
			</div>
			{#if changeError}
				<p class="text-xs text-amber-300" role="alert">{changeError}</p>
			{:else if !change}
				<p class="text-sm text-dark-400" data-testid="change-pending">
					Not applied yet. A change is recorded once the merged pull request is synced and the workload rolls out.
				</p>
			{:else}
				<dl class="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
					<div class="col-span-2"><dt class="text-dark-500">Workload</dt><dd class="text-dark-100 break-all">{changeTarget(change)}</dd></div>
					<div><dt class="text-dark-500">Rolled out</dt><dd class="text-dark-200">{formatDate(change.rollout_completed_at)}</dd></div>
					<div><dt class="text-dark-500">Applied by</dt><dd class="text-dark-200">{change.application_method ?? '—'}</dd></div>
					{#if change.post_window}
						<div class="col-span-2"><dt class="text-dark-500">Measurement window</dt><dd class="text-dark-200">{formatDate(change.post_window.start)} → {formatDate(change.post_window.end)}</dd></div>
					{/if}
				</dl>
				{#if current}
					<MeasurementTable measurement={current} />
					<!-- WP-08.1: released capacity, decided from the specs alone, never money -->
					<ReleasedCapacity capacity={current.capacity} testid="measured-capacity" />
					<!-- MEA-05: why a cost verdict lost hours -->
					<div class="space-y-1 text-xs" data-testid="untrusted-cost-hours">
						<p class="text-dark-500">Workload hours without trusted cost (configured rates, missing or estimated cost)</p>
						<dl class="grid grid-cols-2 gap-2">
							<div><dt class="text-dark-500">Measurement window</dt><dd class="text-dark-200" data-testid="untrusted-cost-hours-post">{formatUntrustedHours(current.untrusted_cost_hours)}</dd></div>
							<div><dt class="text-dark-500">Baseline window</dt><dd class="text-dark-200" data-testid="untrusted-cost-hours-baseline">{formatUntrustedHours(current.baseline_untrusted_cost_hours)}</dd></div>
						</dl>
					</div>
				{:else}
					<p class="text-sm text-dark-400">No measurement decided yet.</p>
				{/if}
				<a href={ledgerHref} class="text-xs text-green-400 hover:text-green-300">Ledger entries of this change →</a>
			{/if}
		</section>
	{/if}
</div>
