<script>
	/** One change on the board (K4): plan, pull request, change and recommendation links. */
	import StatusPill from '#lib/components/StatusPill.svelte';
	import { formatRelativeTime } from '#lib/utils/format.js';

	/** @type {ReturnType<typeof import('#lib/changes.js').buildChangeBoard>[number]} */
	export let row;

	$: title = row.workload ?? (row.planId ? `Plan ${row.planId}` : `Change ${row.changeId}`);
</script>

<article
	class="rounded-lg border bg-dark-950/40 p-3 space-y-2 {row.attention ? 'border-red-700/50' : 'border-dark-700/50'}"
	data-testid="change-row"
	data-key={row.key}
	aria-labelledby="row-{row.key}"
>
	<!-- Wraps: a long status pill moves under the title instead of squeezing it to one letter per line. -->
	<div class="flex flex-wrap items-start justify-between gap-x-2 gap-y-1">
		<h3 id="row-{row.key}" class="min-w-[8rem] flex-1 text-sm font-medium text-dark-100 break-words">
			{#if row.href}
				<a href={row.href} class="hover:text-green-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-green-500/60 rounded">{title}</a>
			{:else}
				{title}
			{/if}
		</h3>
		<StatusPill label={row.statusLabel} tone={row.tone} />
	</div>
	{#if row.namespace || row.repo}
		<p class="text-[11px] text-dark-500 break-all">
			{#if row.namespace}<span>{row.namespace}</span>{/if}{#if row.namespace && row.repo}<span aria-hidden="true"> · </span>{/if}{#if row.repo}<span>{row.repo}</span>{/if}
		</p>
	{/if}
	{#if row.attention}
		<p class="text-xs text-red-300" data-testid="row-attention"><span aria-hidden="true">! </span>{row.attention}</p>
	{/if}
	{#if row.sync && !row.changeId}
		<p class="text-[11px] text-dark-400">{row.sync}</p>
	{/if}
	<ul class="flex flex-wrap gap-x-3 gap-y-1 text-xs" aria-label="Links">
		{#if row.prUrl}
			<li>
				<a href={row.prUrl} target="_blank" rel="noopener noreferrer" class="text-green-400 hover:text-green-300">
					PR{row.prNumber ? ` #${row.prNumber}` : ''} <span aria-hidden="true">↗</span><span class="sr-only">(opens in a new tab)</span>
				</a>
			</li>
		{/if}
		{#each row.recommendationIds as id (id)}
			<li><a href="/recommendations/{id}" class="text-green-400 hover:text-green-300">Recommendation #{id}</a></li>
		{/each}
		{#if row.ledgerHref}
			<li><a href={row.ledgerHref} class="text-green-400 hover:text-green-300">Ledger</a></li>
		{/if}
	</ul>
	{#if row.updatedAt}
		<p class="text-[11px] text-dark-500"><time datetime={row.updatedAt}>Updated {formatRelativeTime(row.updatedAt)}</time></p>
	{/if}
</article>
