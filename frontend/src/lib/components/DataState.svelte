<script>
	export let loading = false;
	export let error = '';
	export let empty = false;
	export let emptyMessage = 'No data available';
</script>

{#if loading}
	<div class="flex items-center justify-center py-12">
		<div class="flex flex-col items-center gap-3">
			<div class="w-8 h-8 border-2 border-green-500 border-t-transparent rounded-full animate-spin"></div>
			<span class="text-sm text-dark-400">Loading…</span>
		</div>
	</div>
{:else if error}
	<div class="flex items-center justify-center py-12">
		<div class="flex flex-col items-center gap-3 text-center">
			<span class="text-2xl">⚠️</span>
			<p class="text-sm text-red-400 max-w-md">{error}</p>
		</div>
	</div>
{:else if empty}
	<div class="flex items-center justify-center py-12">
		<div class="flex flex-col items-center gap-3 text-center">
			<span class="text-2xl">📭</span>
			<p class="text-sm text-dark-400">{emptyMessage}</p>
		</div>
	</div>
{:else}
	<slot />
{/if}
