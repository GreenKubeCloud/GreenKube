<script>
	/**
	 * Manual mode (roadmap H9): the kubectl commands and diff to apply a recommendation by
	 * hand, and "Mark as applied", which records the change so it is measured like a merged
	 * pull request. The guide is fetched on demand; nothing here writes to the cluster.
	 */
	import { createEventDispatcher } from 'svelte';
	import { getManualGuide, markAppliedManually } from '$lib/api.js';

	/** @type {number} */
	export let recommendationId;
	/** Show the steps at once instead of behind a button. */
	export let open = false;

	const dispatch = createEventDispatcher();

	let guide = null;
	let loading = false;
	let error = '';
	let confirming = false;
	let marking = false;
	let markError = '';
	let applied = false;
	let copied = -1;
	let loadedFor = null;

	$: if (open && recommendationId && loadedFor !== recommendationId) loadGuide();

	async function loadGuide() {
		loadedFor = recommendationId;
		loading = true;
		error = '';
		try {
			guide = await getManualGuide(recommendationId);
		} catch (e) {
			guide = null;
			error = e?.message || 'The manual steps could not be loaded.';
		} finally {
			loading = false;
		}
	}

	async function copy(index, text) {
		try {
			await navigator.clipboard.writeText(text);
			copied = index;
			setTimeout(() => (copied = copied === index ? -1 : copied), 1500);
		} catch {
			copied = -1;
		}
	}

	async function markApplied() {
		marking = true;
		markError = '';
		try {
			const record = await markAppliedManually(recommendationId);
			applied = true;
			confirming = false;
			dispatch('applied', record);
		} catch (e) {
			markError = e?.message || 'The recommendation could not be marked as applied.';
		} finally {
			marking = false;
		}
	}
</script>

<section class="card space-y-3" aria-labelledby="manual-heading" data-testid="manual-apply" id="manual">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<h2 id="manual-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Apply manually</h2>
		{#if !open}
			<button
				class="text-xs px-3 py-1.5 rounded-lg border border-dark-600 text-dark-300 hover:border-green-600/50 hover:text-green-400"
				on:click={() => (open = true)}
			>Show manual steps</button>
		{/if}
	</div>
	<p class="text-xs text-dark-400">
		For clusters without GitOps: run the commands yourself, then mark the recommendation as applied. GreenKube
		measures the change like a merged pull request.
	</p>

	{#if open}
		{#if loading}
			<p class="text-sm text-dark-400" role="status">Loading the manual steps…</p>
		{:else if error}
			<p class="text-sm text-amber-300" role="alert">{error}</p>
		{:else if guide}
			{#if guide.notes?.length}
				<ul class="space-y-1 text-xs text-yellow-200" aria-label="Before you apply">
					{#each guide.notes as note (note)}
						<li>! {note}</li>
					{/each}
				</ul>
			{/if}
			{#if guide.commands?.length}
				<ol class="space-y-2" aria-label="Commands">
					{#each guide.commands as step, index (step.command)}
						<li class="space-y-1">
							<p class="text-xs text-dark-400">{index + 1}. {step.description}</p>
							<div class="flex items-start gap-2">
								<pre class="flex-1 overflow-x-auto rounded-lg bg-dark-900 p-2 text-[11px] text-dark-100"><code>{step.command}</code></pre>
								<button
									class="text-[11px] px-2 py-1 rounded border border-dark-600 text-dark-300 hover:text-green-400"
									aria-label="Copy command {index + 1}"
									on:click={() => copy(index, step.command)}
								>{copied === index ? 'Copied' : 'Copy'}</button>
							</div>
						</li>
					{/each}
				</ol>
			{:else}
				<p class="text-sm text-dark-300" data-testid="manual-advisory">
					No command for this kind of change: apply it by hand as described above.
				</p>
			{/if}
			{#if guide.diff}
				<details class="text-xs">
					<summary class="cursor-pointer text-dark-400">Diff</summary>
					<pre class="mt-1 overflow-x-auto rounded-lg bg-dark-900 p-2 text-[11px] text-dark-200"><code>{guide.diff}</code></pre>
				</details>
			{/if}

			{#if applied}
				<p class="text-sm text-green-400" role="status" data-testid="manual-applied">
					✓ Marked as applied. The change is measured over the next days on the ledger.
				</p>
			{:else if guide.can_mark_applied}
				{#if confirming}
					<div class="flex flex-wrap items-center gap-2 text-xs" role="group" aria-label="Confirm">
						<span class="text-dark-300">Is the change live in the cluster?</span>
						<button
							class="px-3 py-1.5 rounded-lg bg-green-600 text-white disabled:opacity-50"
							disabled={marking}
							on:click={markApplied}
						>{marking ? 'Recording…' : 'Yes, mark as applied'}</button>
						<button class="px-3 py-1.5 rounded-lg border border-dark-600 text-dark-300" on:click={() => (confirming = false)}
							>Cancel</button
						>
					</div>
				{:else}
					<button
						class="text-xs px-3 py-1.5 rounded-lg border border-green-600/50 text-green-400 hover:bg-green-600/10"
						on:click={() => (confirming = true)}
					>Mark as applied</button>
				{/if}
				{#if markError}<p class="text-xs text-amber-300" role="alert">{markError}</p>{/if}
			{/if}
		{/if}
	{/if}
</section>
