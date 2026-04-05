<script>
	/**
	 * Color-coded health badge for a service.
	 * @type {'healthy' | 'degraded' | 'unreachable' | 'unconfigured'}
	 */
	export let status = 'unconfigured';
	/** @type {string} */
	export let label = '';
	/** @type {string} */
	export let tooltip = '';
	/** @type {boolean} */
	export let compact = false;

	const colorMap = {
		healthy: 'bg-green-500',
		degraded: 'bg-yellow-500',
		unreachable: 'bg-red-500',
		unconfigured: 'bg-dark-500'
	};

	const labelMap = {
		healthy: 'Healthy',
		degraded: 'Degraded',
		unreachable: 'Unreachable',
		unconfigured: 'Not Configured'
	};

	const badgeColorMap = {
		healthy: 'badge-green',
		degraded: 'badge-yellow',
		unreachable: 'badge-red',
		unconfigured: 'bg-dark-800/50 text-dark-400 border border-dark-600/30'
	};

	$: dotColor = colorMap[status] || colorMap.unconfigured;
	$: statusLabel = labelMap[status] || 'Unknown';
	$: badgeColor = badgeColorMap[status] || badgeColorMap.unconfigured;
</script>

{#if compact}
	<div class="flex items-center gap-1.5" title={tooltip || `${label}: ${statusLabel}`}>
		<div class="w-2 h-2 rounded-full {dotColor} {status === 'degraded' ? 'animate-pulse' : ''}"></div>
		{#if label}
			<span class="text-xs text-dark-400">{label}</span>
		{/if}
	</div>
{:else}
	<div class="flex items-center gap-2" title={tooltip}>
		<div class="w-2.5 h-2.5 rounded-full {dotColor} {status === 'degraded' ? 'animate-pulse' : ''}"></div>
		<span class="text-sm text-dark-300">{label || statusLabel}</span>
		<span class="badge {badgeColor} text-[10px]">{statusLabel}</span>
	</div>
{/if}
