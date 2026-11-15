<script>
	/**
	 * Warning shown when a memory decrease's OOM history could not be read
	 * (eligibility code `oom_history_unknown`). A person may open the pull request;
	 * automation never approves it. `warning` is the server's eligibility entry:
	 * its message names the cause, its fix says what to check before merging.
	 */
	/** @type {{code: string, message: string, fix: string} | null} */
	export let warning = null;
</script>

{#if warning}
	<div
		role="note"
		data-testid="oom-history-warning"
		class="mt-2 text-xs text-amber-200 bg-amber-600/10 border border-amber-500/40 rounded-lg px-3 py-2 space-y-1"
	>
		<p class="font-semibold text-amber-300">
			<span aria-hidden="true">⚠</span> OOM history unknown: review before merging
		</p>
		{#if warning.message}<p>{warning.message}</p>{/if}
		{#if warning.fix}<p class="text-amber-100/80">{warning.fix}</p>{/if}
		<p class="text-amber-100/60">GreenKube never opens or approves this pull request automatically.</p>
	</div>
{/if}
