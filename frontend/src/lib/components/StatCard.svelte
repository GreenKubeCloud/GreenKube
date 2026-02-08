<script>
	/** @type {string} */
	export let label = '';
	/** @type {string} */
	export let value = '';
	/** @type {string} */
	export let icon = '';
	/** @type {string} */
	export let trend = '';
	/** @type {'green'|'red'|'yellow'|'blue'|'default'} */
	export let color = 'default';

	const colorMap = {
		green: 'text-green-400',
		red: 'text-red-400',
		yellow: 'text-yellow-400',
		blue: 'text-blue-400',
		default: 'text-dark-100'
	};
</script>

<div class="card-compact flex flex-col gap-1">
	<div class="flex items-center justify-between">
		<span class="stat-label">{label}</span>
		{#if icon}
			<span class="text-xl opacity-60">{icon}</span>
		{/if}
	</div>
	<div class="flex items-end gap-2">
		<span class="stat-value {colorMap[color]}">{value}</span>
		{#if trend}
			<span class="text-xs text-dark-400 mb-1">{trend}</span>
		{/if}
	</div>
</div>
