<script>
	import { onDestroy } from 'svelte';
	import {
		ApiError,
		getRecommendation,
		getRecommendationEvents,
		getRecommendationPullRequests
	} from '$lib/api.js';
	import { formatCO2, formatDate } from '$lib/utils/format.js';
	import { describeExpectedValue, eligibilityOf, formatMoney } from '$lib/utils/decision.js';
	import { groupConsecutiveEvents } from '$lib/utils/lifecycle.js';
	import { etaFrom, targetName } from '$lib/overview.js';
	import {
		consensusOf,
		eventLabel,
		evidenceOf,
		formatConsensusValue,
		impactOf,
		measuredOf,
		recommendationStatus,
		requestRows,
		safeHttpUrl,
		valueOf
	} from '$lib/recommendationDetail.js';
	import StatusPill from '$lib/components/StatusPill.svelte';

	/** @type {{id: string}} */
	export let data;

	let rec = null;
	let events = [];
	let pullRequests = [];
	let loading = true;
	let notFound = false;
	let error = '';
	let eventsError = '';
	let prsError = '';
	let controller = null;
	let loadedId = null;

	onDestroy(() => controller?.abort());

	$: if (data?.id !== loadedId) load(data?.id);

	async function load(id) {
		loadedId = id;
		controller?.abort();
		controller = new AbortController();
		const { signal } = controller;
		loading = true;
		notFound = false;
		error = '';
		rec = null;
		events = [];
		pullRequests = [];
		if (!/^\d+$/.test(String(id ?? ''))) {
			notFound = true;
			loading = false;
			return;
		}
		const [recResult, eventsResult, prsResult] = await Promise.allSettled([
			getRecommendation(id, { signal }),
			getRecommendationEvents(id, { signal }),
			getRecommendationPullRequests(id, { signal })
		]);
		if (signal.aborted) return;
		if (recResult.status === 'rejected') {
			const reason = recResult.reason;
			if (reason instanceof ApiError && reason.status === 404) notFound = true;
			else error = reason?.message || 'The recommendation could not be loaded.';
		} else {
			rec = recResult.value;
		}
		events = eventsResult.status === 'fulfilled' && Array.isArray(eventsResult.value) ? eventsResult.value : [];
		eventsError = eventsResult.status === 'rejected' ? 'The lifecycle events could not be loaded.' : '';
		pullRequests = prsResult.status === 'fulfilled' && Array.isArray(prsResult.value) ? prsResult.value : [];
		prsError = prsResult.status === 'rejected' ? 'The pull requests could not be loaded.' : '';
		loading = false;
	}

	$: status = recommendationStatus(rec?.status);
	$: eligibility = eligibilityOf(rec);
	$: impact = impactOf(rec);
	$: value = valueOf(rec);
	$: evidence = evidenceOf(rec);
	$: consensus = consensusOf(rec);
	$: measured = measuredOf(rec);
	$: requests = requestRows(rec);
	$: timeline = groupConsecutiveEvents(events);
	$: planIds = [...new Set(pullRequests.map((pr) => pr.plan_id).filter(Boolean))];
	$: currency = value?.currency ?? impact?.currency ?? 'USD';

	function pct(value) {
		return value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`;
	}

	function cores(millicores) {
		return millicores === null || millicores === undefined ? '—' : `${Math.round(millicores)}m`;
	}

	function mib(bytes) {
		return bytes === null || bytes === undefined ? '—' : `${Math.round(bytes / (1024 * 1024))}Mi`;
	}

	function prTone(state) {
		return { open: 'blue', merged: 'green', closed: 'dark', error: 'red', pending: 'yellow' }[state] ?? 'dark';
	}
</script>

<svelte:head><title>Recommendation {data?.id} · GreenKube</title></svelte:head>

<div class="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1200px] mx-auto">
	<nav aria-label="Breadcrumb" class="text-xs text-dark-500">
		<a href="/recommendations" class="hover:text-dark-200">Recommendations</a>
		<span aria-hidden="true"> / </span>
		<span aria-current="page">#{data?.id}</span>
	</nav>

	{#if loading}
		<p class="text-sm text-dark-400" role="status">Loading recommendation…</p>
	{:else if notFound}
		<div class="card text-center space-y-2" data-testid="not-found">
			<h1 class="text-lg font-semibold text-dark-100">Recommendation not found</h1>
			<p class="text-sm text-dark-400">
				Recommendation #{data?.id} does not exist (it may have been superseded by a newer scan and pruned).
			</p>
			<a href="/recommendations" class="text-sm text-green-400 hover:text-green-300">Back to recommendations</a>
		</div>
	{:else if error}
		<div class="card text-sm text-amber-300" role="alert">{error}</div>
	{:else if rec}
		<!-- Header -->
		<header class="space-y-2">
			<div class="flex flex-wrap items-center gap-2">
				<h1 class="text-2xl font-bold text-dark-100 break-all">{targetName(rec)}</h1>
				<StatusPill label={status.label} tone={status.tone} />
				{#if consensus?.revision !== null && consensus?.revision !== undefined}
					<StatusPill label="Revision {consensus.revision}" tone="dark" />
				{:else if typeof rec.revision === 'number'}
					<StatusPill label="Revision {rec.revision}" tone="dark" />
				{/if}
			</div>
			<p class="text-sm text-dark-400">
				{rec.type?.replaceAll('_', ' ').toLowerCase()}
				{#if rec.namespace}· namespace <span class="text-dark-200">{rec.namespace}</span>{/if}
				{#if rec.container_name}· container <span class="text-dark-200">{rec.container_name}</span>{/if}
				· source <span class="text-dark-200">{rec.source}</span>
				{#if rec.risk_level}· {rec.risk_level} risk{/if}
			</p>
			{#if rec.reason || rec.description}
				<p class="text-sm text-dark-300">{rec.reason || rec.description}</p>
			{/if}
		</header>

		<div class="grid grid-cols-1 gap-6 lg:grid-cols-2">
			<!-- Proposal and projected value -->
			<section class="card space-y-3" aria-labelledby="proposal-heading">
				<h2 id="proposal-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Proposal</h2>
				{#if requests.length}
					<table class="w-full text-sm">
						<caption class="sr-only">Current and proposed requests</caption>
						<thead>
							<tr class="text-left text-xs text-dark-500">
								<th scope="col" class="py-1 font-medium">Resource</th>
								<th scope="col" class="py-1 font-medium">Current</th>
								<th scope="col" class="py-1 font-medium">Proposed</th>
							</tr>
						</thead>
						<tbody>
							{#each requests as row}
								<tr class="border-t border-dark-700/50">
									<th scope="row" class="py-1 text-left font-normal text-dark-400">{row.resource}</th>
									<td class="py-1 text-dark-200">{row.current}</td>
									<td class="py-1 text-dark-100 font-medium">{row.proposed}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				{:else if rec.cron_schedule}
					<p class="text-sm text-dark-300">Schedule: <code>{rec.cron_schedule}</code></p>
				{:else}
					<p class="text-sm text-dark-400">{rec.description}</p>
				{/if}
				{#if value}
					<dl class="grid grid-cols-2 gap-2 text-xs" data-testid="value-block">
						<div><dt class="text-dark-500">Expected value</dt><dd class="text-dark-100">{describeExpectedValue(rec) ?? '—'}</dd></div>
						<div><dt class="text-dark-500">Projected value</dt><dd class="text-dark-200">{formatMoney(value.valueAnnual, currency)}/yr</dd></div>
						<div><dt class="text-dark-500">Success probability</dt><dd class="text-dark-200">{pct(value.pSuccess)}</dd></div>
						<div><dt class="text-dark-500">Incident cost (risk)</dt><dd class="text-dark-200">{formatMoney(value.incidentCost, currency)}</dd></div>
						<div><dt class="text-dark-500">Effort cost</dt><dd class="text-dark-200">{formatMoney(value.effortCost, currency)}</dd></div>
						{#if value.lane === 'reliability'}
							<div><dt class="text-dark-500">Lane</dt><dd class="text-purple-300">Reliability (adds resources)</dd></div>
						{/if}
					</dl>
				{/if}
			</section>

			<!-- Eligibility -->
			<section class="card space-y-3" aria-labelledby="eligibility-heading" data-testid="eligibility">
				<h2 id="eligibility-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Delivery</h2>
				{#if !eligibility}
					<p class="text-sm text-dark-400">The server returned no delivery eligibility for this recommendation.</p>
				{:else if eligibility.canOpenPr}
					<p class="text-sm text-green-400">✓ Can become a pull request ({eligibility.deliveryModes.join(', ') || 'direct'}).</p>
					<a href="/recommendations" class="text-xs text-green-400 hover:text-green-300">Preview the plan from the recommendations list →</a>
				{:else}
					<p class="text-sm text-dark-300">✕ Cannot become a pull request yet.</p>
				{/if}
				{#if eligibility?.eligibleAt && etaFrom(eligibility.eligibleAt)}
					<p class="text-xs text-blue-200" role="status">Collecting evidence — eligible in {etaFrom(eligibility.eligibleAt)}.</p>
				{/if}
				{#if eligibility?.blockers.length}
					<ul class="space-y-2" aria-label="Blockers">
						{#each eligibility.blockers as blocker (blocker.code + blocker.message)}
							<li class="rounded-lg border border-dark-700/50 bg-dark-800/60 p-3 text-xs">
								<p class="text-dark-100"><span class="font-mono text-dark-400">{blocker.code}</span> · {blocker.message}</p>
								{#if blocker.fix}<p class="mt-1 text-green-300">Fix: {blocker.fix}</p>{/if}
							</li>
						{/each}
					</ul>
				{/if}
				{#if eligibility?.warnings.length}
					<ul class="space-y-1 text-xs text-yellow-300" aria-label="Warnings">
						{#each eligibility.warnings as warning (warning.code + warning.message)}
							<li>! {warning.message || warning.code}{#if warning.fix} — {warning.fix}{/if}</li>
						{/each}
					</ul>
				{/if}
			</section>
		</div>

		<!-- Projected impact -->
		{#if impact}
			<section class="card space-y-4" aria-labelledby="impact-heading" data-testid="impact">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<h2 id="impact-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Projected impact</h2>
					<div class="flex items-center gap-2">
						<StatusPill label="Projected" tone="blue" icon="≈" />
						{#if impact.modelVersion}<span class="text-xs text-dark-500">Impact Model {impact.modelVersion}</span>{/if}
					</div>
				</div>
				<dl class="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
					<div><dt class="text-dark-500">Allocated cost</dt><dd class="text-dark-100">{formatMoney(impact.allocatedCostAnnual, impact.currency)}/yr</dd></div>
					<div><dt class="text-dark-500">Expected billable cost</dt><dd class="text-dark-100">{formatMoney(impact.expectedCostAnnual, impact.currency)}/yr</dd></div>
					<div><dt class="text-dark-500">Realization factor</dt><dd class="text-dark-100">{pct(impact.realizationFactor)}</dd></div>
					<div>
						<dt class="text-dark-500">Expected CO₂e</dt>
						<dd class="text-dark-100">{impact.co2eModeled ? `${formatCO2(impact.co2eExpectedAnnualG)}/yr` : 'Not modeled'}</dd>
					</div>
				</dl>
				<div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
					{#if impact.costComponents.length}
						<div>
							<h3 class="text-xs font-semibold text-dark-400 mb-1">Cost by component (allocated, per year)</h3>
							<ul class="text-xs space-y-0.5">
								{#each impact.costComponents as row (row.key)}
									<li class="flex justify-between gap-2"><span class="text-dark-400">{row.label}</span><span class="text-dark-200">{formatMoney(row.value, impact.currency)}</span></li>
								{/each}
							</ul>
						</div>
					{/if}
					{#if impact.co2eComponents.length}
						<div>
							<h3 class="text-xs font-semibold text-dark-400 mb-1">CO₂e by component (allocated, per year)</h3>
							<ul class="text-xs space-y-0.5">
								{#each impact.co2eComponents as row (row.key)}
									<li class="flex justify-between gap-2"><span class="text-dark-400">{row.label}</span><span class="text-dark-200">{formatCO2(row.value)}</span></li>
								{/each}
							</ul>
						</div>
					{/if}
				</div>
				{#if impact.assumptions.length}
					<details class="text-xs">
						<summary class="cursor-pointer text-dark-400 hover:text-dark-200">Assumptions</summary>
						<dl class="mt-2 grid grid-cols-1 gap-1 sm:grid-cols-2">
							{#each impact.assumptions as [key, val] (key)}
								<div class="flex justify-between gap-2"><dt class="text-dark-500">{key.replaceAll('_', ' ')}</dt><dd class="text-dark-200 break-all">{String(val)}</dd></div>
							{/each}
						</dl>
					</details>
				{/if}
			</section>
		{/if}

		<!-- Consensus across sources (Recommendation v2 only) -->
		{#if consensus}
			<section class="card space-y-3" aria-labelledby="consensus-heading" data-testid="consensus">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<h2 id="consensus-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Sources and consensus</h2>
					{#if consensus.contested}
						<StatusPill label="Contested" tone="yellow" icon="!" />
					{:else if consensus.agreement !== null}
						<StatusPill label="Agreement {pct(consensus.agreement)}" tone="green" />
					{/if}
				</div>
				<p class="text-xs text-dark-400">
					Policy <span class="text-dark-200">{consensus.policy ?? 'unknown'}</span>
					{#if consensus.resource && consensus.value !== null}
						· chose <span class="text-dark-100 font-medium">{formatConsensusValue(consensus.resource, consensus.value)}</span>
						for {consensus.resource.replaceAll('_', ' ')}
					{/if}
					{#if consensus.chosenFrom.length}· supported by <span class="text-dark-200">{consensus.chosenFrom.join(', ')}</span>{/if}
					{#if consensus.independentVotes !== null}· {consensus.independentVotes} independent {consensus.independentVotes === 1 ? 'vote' : 'votes'}{/if}
				</p>
				{#if consensus.spread.length}
					<ul class="space-y-1 text-xs" aria-label="Proposals per source">
						{#each consensus.spread as row (row.resource)}
							<li>
								<span class="text-dark-400">{row.resource.replaceAll('_', ' ')}:</span>
								{#each row.values as item, i (item.source)}
									<span class="text-dark-200">{item.source} {item.display}</span>{#if i < row.values.length - 1}<span class="text-dark-500"> · </span>{/if}
								{/each}
							</li>
						{/each}
					</ul>
				{/if}
				{#if consensus.votes.length}
					<div class="overflow-x-auto">
						<table class="w-full min-w-[420px] text-xs" data-testid="consensus-votes">
							<caption class="sr-only">Signals that took part in the consensus</caption>
							<thead>
								<tr class="text-left text-dark-500">
									<th scope="col" class="py-1 font-medium">Source</th>
									<th scope="col" class="py-1 font-medium">Proposal</th>
									<th scope="col" class="py-1 font-medium">Vote</th>
								</tr>
							</thead>
							<tbody>
								{#each consensus.votes as item, i (item.sourceId + i)}
									<tr class="border-t border-dark-700/50">
										<th scope="row" class="py-1 text-left font-normal text-dark-200">
											{item.sourceId}{#if item.family && item.family !== item.sourceId}<span class="text-dark-500"> ({item.family})</span>{/if}
										</th>
										<td class="py-1 text-dark-200">
											{item.value === null ? '—' : formatConsensusValue(consensus.resource, item.value)}{#if item.direction} <span class="text-dark-500">{item.direction}</span>{/if}
										</td>
										<td class="py-1 text-dark-300">
											{item.counted ? 'Counted' : 'Same engine as another source, not counted'}{#if item.supportsChosen} · supports the choice{/if}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}
				{#if consensus.reasons.length}
					<ul class="list-disc pl-4 text-xs text-yellow-200" aria-label="Why the consensus is contested">
						{#each consensus.reasons as reason}<li>{reason}</li>{/each}
					</ul>
				{/if}
			</section>
		{/if}

		<!-- Evidence -->
		<section class="card space-y-3" aria-labelledby="evidence-heading" data-testid="evidence">
			<h2 id="evidence-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Evidence</h2>
			{#if evidence}
				<dl class="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
					<div><dt class="text-dark-500">Window</dt><dd class="text-dark-200">{formatDate(evidence.windowStart)} → {formatDate(evidence.windowEnd)}</dd></div>
					<div><dt class="text-dark-500">Samples</dt><dd class="text-dark-200">{evidence.samples ?? '—'}</dd></div>
					<div><dt class="text-dark-500">Coverage</dt><dd class="text-dark-200">{pct(evidence.coverage)}</dd></div>
					<div><dt class="text-dark-500">Restarts / OOM kills</dt><dd class="text-dark-200">{evidence.restarts ?? '—'} / {evidence.oomEvents ?? '—'}</dd></div>
				</dl>
				{#if evidence.cpu || evidence.memory}
					<table class="w-full text-xs">
						<caption class="sr-only">Observed usage</caption>
						<thead>
							<tr class="text-left text-dark-500">
								<th scope="col" class="py-1 font-medium">Usage</th>
								<th scope="col" class="py-1 font-medium">p50</th>
								<th scope="col" class="py-1 font-medium">p95</th>
								<th scope="col" class="py-1 font-medium">p99</th>
								<th scope="col" class="py-1 font-medium">max</th>
							</tr>
						</thead>
						<tbody>
							{#if evidence.cpu}
								<tr class="border-t border-dark-700/50">
									<th scope="row" class="py-1 text-left font-normal text-dark-400">CPU</th>
									<td class="text-dark-200">{cores(evidence.cpu.p50)}</td>
									<td class="text-dark-200">{cores(evidence.cpu.p95)}</td>
									<td class="text-dark-200">{cores(evidence.cpu.p99)}</td>
									<td class="text-dark-200">{cores(evidence.cpu.max)}</td>
								</tr>
							{/if}
							{#if evidence.memory}
								<tr class="border-t border-dark-700/50">
									<th scope="row" class="py-1 text-left font-normal text-dark-400">Memory</th>
									<td class="text-dark-200">{mib(evidence.memory.p50)}</td>
									<td class="text-dark-200">{mib(evidence.memory.p95)}</td>
									<td class="text-dark-200">{mib(evidence.memory.p99)}</td>
									<td class="text-dark-200">{mib(evidence.memory.max)}</td>
								</tr>
							{/if}
						</tbody>
					</table>
				{/if}
				{#if evidence.riskFactors.length}
					<p class="text-xs text-dark-400">Risk factors: <span class="text-dark-200">{evidence.riskFactors.join(', ')}</span></p>
				{/if}
				{#if evidence.rollbackConditions.length}
					<div>
						<h3 class="text-xs font-semibold text-dark-400">Rollback conditions</h3>
						<ul class="text-xs text-dark-300 list-disc pl-4">
							{#each evidence.rollbackConditions as condition}
								<li>{condition.description || `${condition.metric} ${condition.comparator} ${condition.threshold}`}</li>
							{/each}
						</ul>
					</div>
				{/if}
				{#if evidence.savingsMethod}
					<p class="text-xs text-dark-500">Savings method: {evidence.savingsMethod}</p>
				{/if}
				{#if evidence.diff}
					<details class="text-xs">
						<summary class="cursor-pointer text-dark-400 hover:text-dark-200">Proposed diff</summary>
						<pre class="mt-2 max-h-80 overflow-auto rounded-lg bg-dark-950 p-3 text-dark-200">{evidence.diff}</pre>
					</details>
				{/if}
			{:else}
				<p class="text-sm text-dark-400">This recommendation carries no evidence block.</p>
			{/if}
		</section>

		<div class="grid grid-cols-1 gap-6 lg:grid-cols-2">
			<!-- Pull requests and changes -->
			<section class="card space-y-3" aria-labelledby="prs-heading" data-testid="pull-requests">
				<h2 id="prs-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Pull requests</h2>
				{#if prsError}
					<p class="text-xs text-amber-300" role="alert">{prsError}</p>
				{:else if !pullRequests.length}
					<p class="text-sm text-dark-400">No pull request yet.</p>
				{:else}
					<ul class="space-y-2">
						{#each pullRequests as pr (pr.id ?? pr.plan_id ?? pr.pr_url)}
							<li class="flex flex-wrap items-center gap-2 text-sm">
								<StatusPill label={pr.status} tone={prTone(pr.status)} />
								{#if safeHttpUrl(pr.pr_url)}
									<a href={safeHttpUrl(pr.pr_url)} target="_blank" rel="noopener noreferrer" class="text-green-400 hover:text-green-300">
										{pr.repo}{pr.pr_number ? ` #${pr.pr_number}` : ''}
									</a>
								{:else}
									<span class="text-dark-300">{pr.repo}</span>
								{/if}
								{#if pr.sync_status}<span class="text-xs text-dark-500">sync: {pr.sync_status}</span>{/if}
								{#if pr.plan_id}
									<a href="/changes/{encodeURIComponent(pr.plan_id)}" class="text-xs text-dark-300 hover:text-green-300">Change →</a>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<!-- Measurement -->
			<section class="card space-y-3" aria-labelledby="measurement-heading" data-testid="measurement">
				<div class="flex flex-wrap items-center justify-between gap-2">
					<h2 id="measurement-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Measurement</h2>
					{#if measured}<StatusPill label="Measured" tone="green" icon="✓" />{/if}
				</div>
				{#if measured}
					<dl class="grid grid-cols-2 gap-2 text-xs">
						<div><dt class="text-dark-500">Measured cost saving</dt><dd class="text-dark-100">{measured.costAnnual === null ? '—' : `${formatMoney(measured.costAnnual, currency)}/yr`}</dd></div>
						<div><dt class="text-dark-500">Measured CO₂e saving</dt><dd class="text-dark-100">{measured.co2eAnnualG === null ? '—' : `${formatCO2(measured.co2eAnnualG)}/yr`}</dd></div>
						<div><dt class="text-dark-500">Verification</dt><dd class="text-dark-200">{measured.status ?? '—'}</dd></div>
						<div><dt class="text-dark-500">Realized</dt><dd class="text-dark-200">{measured.realized === null ? '—' : measured.realized ? 'Yes' : 'No'}</dd></div>
					</dl>
				{:else}
					<p class="text-sm text-dark-400">Not measured yet. Measurement starts after the change is applied and warmed up.</p>
				{/if}
				{#if planIds.length}
					<ul class="text-xs space-y-1">
						{#each planIds as planId (planId)}
							<li><a href="/ledger?change={encodeURIComponent(planId)}" class="text-green-400 hover:text-green-300">Ledger entries of plan {planId} →</a></li>
						{/each}
					</ul>
				{:else}
					<a href="/ledger" class="text-xs text-green-400 hover:text-green-300">Open the savings ledger →</a>
				{/if}
			</section>
		</div>

		<!-- Lifecycle timeline -->
		<section class="card space-y-3" aria-labelledby="timeline-heading" data-testid="timeline">
			<h2 id="timeline-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Lifecycle</h2>
			{#if eventsError}
				<p class="text-xs text-amber-300" role="alert">{eventsError}</p>
			{:else if !timeline.length}
				<p class="text-sm text-dark-400">No lifecycle event recorded.</p>
			{:else}
				<ol class="relative border-l border-dark-700 pl-4 space-y-3">
					{#each timeline as group, i (i)}
						<li>
							<p class="text-sm text-dark-100">
								{eventLabel(group.event_type)}
								{#if group.count > 1}<span class="text-xs text-dark-500">×{group.count}</span>{/if}
							</p>
							<p class="text-xs text-dark-500">
								<time datetime={group.last.created_at}>{formatDate(group.last.created_at)}</time> · {group.actor}
							</p>
							{#if group.last.payload?.details?.reason}
								<p class="text-xs text-dark-400">{group.last.payload.details.reason}</p>
							{/if}
						</li>
					{/each}
				</ol>
			{/if}
		</section>
	{/if}
</div>
