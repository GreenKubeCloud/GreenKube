<script>
	import { onMount } from 'svelte';
	import { selectedNamespace } from '$lib/stores.js';
	import { getRecommendations } from '$lib/api.js';
	import { formatCO2, formatCost, formatCPU, formatBytes } from '$lib/utils/format.js';
	import DataState from '$lib/components/DataState.svelte';
	import Card from '$lib/components/Card.svelte';

	let recommendations = [];
	let loading = true;
	let error = null;
	let filterType = 'all';

	$: nsParam = $selectedNamespace;
	$: if (nsParam !== undefined) loadData();

	async function loadData() {
		loading = true;
		error = null;
		try {
			recommendations = await getRecommendations({ namespace: $selectedNamespace || undefined });
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	$: types = [...new Set(recommendations.map(r => r.type))];

	$: filtered = filterType === 'all'
		? recommendations
		: recommendations.filter(r => r.type === filterType);

	$: totalSavingsCO2 = filtered.reduce((s, r) => s + (r.potential_savings_co2e_grams ?? 0), 0);
	$: totalSavingsCost = filtered.reduce((s, r) => s + (r.potential_savings_cost ?? 0), 0);

	const typeConfig = {
		ZOMBIE_POD: { icon: '💀', label: 'Zombie Pod', color: 'red', desc: 'Pod with no meaningful activity' },
		RIGHTSIZING_CPU: { icon: '📐', label: 'CPU Rightsizing', color: 'yellow', desc: 'CPU request can be optimized' },
		RIGHTSIZING_MEMORY: { icon: '📐', label: 'Memory Rightsizing', color: 'yellow', desc: 'Memory request can be optimized' },
		AUTOSCALING_CANDIDATE: { icon: '📈', label: 'Autoscaling', color: 'orange', desc: 'Workload with spiky usage — consider HPA' },
		OFF_PEAK_SCALING: { icon: '🌙', label: 'Off-Peak Scaling', color: 'indigo', desc: 'Idle during off-peak hours — scale down with cron' },
		IDLE_NAMESPACE: { icon: '💤', label: 'Idle Namespace', color: 'purple', desc: 'Namespace with minimal activity' },
		CARBON_AWARE_SCHEDULING: { icon: '🌍', label: 'Carbon-Aware', color: 'green', desc: 'Could run in a lower-carbon zone' },
		OVERPROVISIONED_NODE: { icon: '🖥️', label: 'Overprovisioned Node', color: 'blue', desc: 'Node with very low utilization' },
		UNDERUTILIZED_NODE: { icon: '🔻', label: 'Underutilized Node', color: 'blue', desc: 'Node with few pods — consider draining' },
	};

	function getTypeConfig(type) {
		return typeConfig[type] ?? { icon: '❓', label: type, color: 'blue', desc: '' };
	}

	onMount(() => loadData());
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<!-- Header -->
	<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Recommendations</h1>
			<p class="text-sm text-dark-500 mt-1">
				Actionable suggestions to reduce your cluster's environmental footprint
			</p>
		</div>
		<!-- Namespace filter -->
		{#if $selectedNamespace}
			<button
				class="btn-secondary text-xs"
				on:click={() => selectedNamespace.set('')}
			>
				Clear filter: {$selectedNamespace} ✕
			</button>
		{/if}
	</div>

	<DataState {loading} {error} empty={!recommendations.length} emptyMessage="No recommendations — your cluster looks great! 🎉">
		<!-- Potential savings -->
		<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
			<div class="card-compact text-center">
				<p class="stat-label">Recommendations</p>
				<p class="stat-value text-2xl">{filtered.length}</p>
			</div>
			<div class="card-compact text-center">
				<p class="stat-label">Potential CO₂ Savings</p>
				<p class="stat-value text-2xl text-green-400">{formatCO2(totalSavingsCO2)}</p>
			</div>
			<div class="card-compact text-center">
				<p class="stat-label">Potential Cost Savings</p>
				<p class="stat-value text-2xl text-blue-400">{formatCost(totalSavingsCost)}</p>
			</div>
		</div>

		<!-- Type filter tabs -->
		{#if types.length > 1}
			<div class="flex flex-wrap gap-2">
				<button
					class="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors
					       {filterType === 'all' ? 'bg-green-600/20 text-green-400' : 'bg-dark-800 text-dark-400 hover:text-dark-200'}"
					on:click={() => filterType = 'all'}
				>
					All ({recommendations.length})
				</button>
				{#each types as type}
					{@const cfg = getTypeConfig(type)}
					{@const count = recommendations.filter(r => r.type === type).length}
					<button
						class="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors
						       {filterType === type ? `bg-${cfg.color}-600/20 text-${cfg.color}-400` : 'bg-dark-800 text-dark-400 hover:text-dark-200'}"
						on:click={() => filterType = type}
					>
						{cfg.icon} {cfg.label} ({count})
					</button>
				{/each}
			</div>
		{/if}

		<!-- Recommendation cards -->
		<div class="space-y-3">
			{#each filtered as rec}
				{@const cfg = getTypeConfig(rec.type)}
				<div class="card hover:border-{cfg.color}-600/30 transition-all duration-200">
					<div class="flex items-start gap-4">
						<!-- Icon -->
						<div class="text-2xl flex-shrink-0 mt-0.5">{cfg.icon}</div>

						<!-- Content -->
						<div class="flex-1 min-w-0">
							<div class="flex items-start justify-between gap-3">
								<div class="min-w-0">
									<div class="flex items-center gap-2 flex-wrap">
										<h3 class="text-sm font-semibold text-dark-100">{rec.pod_name}</h3>
										<span class="badge-{cfg.color} text-[10px]">{cfg.label}</span>
									</div>
									{#if rec.namespace}
										<p class="text-xs text-dark-500 mt-0.5">
											Namespace: <span class="text-dark-400">{rec.namespace}</span>
										</p>
									{/if}
								</div>
								<!-- Savings -->
								{#if rec.potential_savings_co2e_grams || rec.potential_savings_cost}
									<div class="text-right flex-shrink-0">
										{#if rec.potential_savings_co2e_grams}
											<p class="text-sm font-bold text-green-400">
												-{formatCO2(rec.potential_savings_co2e_grams)}
											</p>
										{/if}
										{#if rec.potential_savings_cost}
											<p class="text-xs text-blue-400">
												-{formatCost(rec.potential_savings_cost)}
											</p>
										{/if}
									</div>
								{/if}
							</div>

							<!-- Reason -->
							<p class="text-sm text-dark-400 mt-2">{rec.reason}</p>

							<!-- Details -->
							{#if rec.current_cpu_request_millicores || rec.current_memory_request_bytes}
								<div class="mt-3 flex flex-wrap gap-4">
									{#if rec.current_cpu_request_millicores != null}
										<div class="flex items-center gap-2">
											<span class="text-[10px] uppercase text-dark-600">CPU Req</span>
											<span class="text-xs text-dark-400 font-mono">{formatCPU(rec.current_cpu_request_millicores)}</span>
											{#if rec.recommended_cpu_request_millicores != null}
												<span class="text-dark-600">→</span>
												<span class="text-xs text-green-400 font-mono">{formatCPU(rec.recommended_cpu_request_millicores)}</span>
											{/if}
										</div>
									{/if}
									{#if rec.current_memory_request_bytes != null}
										<div class="flex items-center gap-2">
											<span class="text-[10px] uppercase text-dark-600">Mem Req</span>
											<span class="text-xs text-dark-400 font-mono">{formatBytes(rec.current_memory_request_bytes)}</span>
											{#if rec.recommended_memory_request_bytes != null}
												<span class="text-dark-600">→</span>
												<span class="text-xs text-green-400 font-mono">{formatBytes(rec.recommended_memory_request_bytes)}</span>
											{/if}
										</div>
									{/if}
								</div>
							{/if}

							<!-- Cron schedule (off-peak) -->
							{#if rec.cron_schedule}
								<div class="mt-2 flex items-center gap-2">
									<span class="text-[10px] uppercase text-dark-600">Cron Schedule</span>
									<code class="text-xs text-indigo-400 bg-dark-800 px-2 py-0.5 rounded font-mono">{rec.cron_schedule}</code>
								</div>
							{/if}

							<!-- Target node -->
							{#if rec.target_node}
								<div class="mt-2 flex items-center gap-2">
									<span class="text-[10px] uppercase text-dark-600">Node</span>
									<span class="text-xs text-dark-300 font-mono">{rec.target_node}</span>
								</div>
							{/if}

							<!-- Priority badge -->
							{#if rec.priority}
								<div class="mt-2">
									<span class="text-[10px] uppercase px-2 py-0.5 rounded
										{rec.priority === 'high' ? 'bg-red-600/20 text-red-400' :
										 rec.priority === 'medium' ? 'bg-yellow-600/20 text-yellow-400' :
										 'bg-dark-700 text-dark-400'}">
										{rec.priority} priority
									</span>
								</div>
							{/if}
						</div>
					</div>
				</div>
			{/each}
		</div>
	</DataState>
</div>
