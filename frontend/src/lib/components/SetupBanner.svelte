<script>
	import { createEventDispatcher } from 'svelte';

	/** Normalized setup status (see #lib/setup.js normalizeSetupStatus). */
	export let status = null;

	const dispatch = createEventDispatcher();

	$: count = status?.actionCount ?? 0;
</script>

{#if status && status.complete === false}
	<div
		class="flex flex-col gap-2 border-b border-yellow-700/40 bg-yellow-900/30 px-4 py-2 text-xs text-yellow-100 sm:flex-row sm:items-center sm:justify-between"
		role="region"
		aria-label="Setup status"
		data-testid="setup-banner"
	>
		<p>
			<span class="font-semibold">Setup is not complete.</span>
			{#if status.deliveryPaused}
				Delivery is paused by policy (automation.policy.pauseDelivery): no pull request is opened.
			{:else if count > 0}
				{count} {count === 1 ? 'check needs' : 'checks need'} your attention before GreenKube can open pull requests.
			{:else}
				Some checks are still pending.
			{/if}
		</p>
		<div class="flex shrink-0 items-center gap-3">
			<a href="/setup" class="font-medium text-yellow-200 underline underline-offset-2 hover:text-white">
				Open the setup checklist
			</a>
			<button
				type="button"
				class="rounded px-2 py-0.5 text-yellow-200 hover:bg-yellow-800/40 hover:text-white focus:outline-none focus:ring-2 focus:ring-yellow-500/50"
				aria-label="Dismiss the setup banner"
				on:click={() => dispatch('dismiss')}
			>
				Dismiss
			</button>
		</div>
	</div>
{/if}
