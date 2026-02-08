<script>
	/** @type {string} */
	export let title = '';
	/** @type {string} */
	export let subtitle = '';
	/** @type {string} */
	export let icon = '';
	/** @type {string} */
	export let className = '';
</script>

<div class="card {className}">
	{#if title || icon}
		<div class="flex items-center justify-between mb-4">
			<div class="flex items-center gap-2">
				{#if icon}
					<span class="text-lg">{icon}</span>
				{/if}
				{#if title}
					<h3 class="text-sm font-semibold text-dark-300 uppercase tracking-wider">{title}</h3>
				{/if}
			</div>
			{#if subtitle}
				<span class="text-xs text-dark-500">{subtitle}</span>
			{/if}
			<slot name="actions" />
		</div>
	{/if}
	<slot />
</div>
