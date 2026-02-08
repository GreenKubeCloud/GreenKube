<script>
	import { onMount } from 'svelte';
	import { getNodes } from '$lib/api.js';
	import { formatCPU, formatBytes, formatNumber } from '$lib/utils/format.js';
	import DataState from '$lib/components/DataState.svelte';
	import Card from '$lib/components/Card.svelte';

	let nodes = [];
	let loading = true;
	let error = null;

	onMount(async () => {
		try {
			nodes = await getNodes();
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	});

	function cpuPercent(node) {
		if (!node.cpu_capacity_millicores || !node.cpu_allocatable_millicores) return null;
		return ((node.cpu_capacity_millicores - node.cpu_allocatable_millicores) / node.cpu_capacity_millicores * 100).toFixed(0);
	}

	function memPercent(node) {
		if (!node.memory_capacity_bytes || !node.memory_allocatable_bytes) return null;
		return ((node.memory_capacity_bytes - node.memory_allocatable_bytes) / node.memory_capacity_bytes * 100).toFixed(0);
	}
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<!-- Header -->
	<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Nodes</h1>
			<p class="text-sm text-dark-500 mt-1">
				Cluster node inventory and capacity
			</p>
		</div>
		{#if nodes.length > 0}
			<div class="flex items-center gap-2">
				<span class="badge-green">{nodes.length} node{nodes.length !== 1 ? 's' : ''}</span>
			</div>
		{/if}
	</div>

	<DataState {loading} {error} empty={!nodes.length} emptyMessage="No nodes discovered">
		<!-- Summary -->
		<div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
			<div class="card-compact text-center">
				<p class="stat-label">Total Nodes</p>
				<p class="stat-value text-xl">{nodes.length}</p>
			</div>
			<div class="card-compact text-center">
				<p class="stat-label">Total CPU</p>
				<p class="stat-value text-xl">
					{formatCPU(nodes.reduce((s, n) => s + (n.cpu_capacity_millicores ?? 0), 0))}
				</p>
			</div>
			<div class="card-compact text-center">
				<p class="stat-label">Total Memory</p>
				<p class="stat-value text-xl">
					{formatBytes(nodes.reduce((s, n) => s + (n.memory_capacity_bytes ?? 0), 0))}
				</p>
			</div>
			<div class="card-compact text-center">
				<p class="stat-label">Architectures</p>
				<p class="stat-value text-xl">
					{[...new Set(nodes.map(n => n.architecture).filter(Boolean))].length}
				</p>
			</div>
		</div>

		<!-- Node Cards -->
		<div class="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
			{#each nodes as node}
				<div class="card group hover:border-green-600/30 transition-all duration-200">
					<!-- Node Header -->
					<div class="flex items-start justify-between mb-4">
						<div class="min-w-0 flex-1">
							<h3 class="text-sm font-semibold text-dark-100 truncate" title={node.name}>
								{node.name}
							</h3>
							{#if node.instance_type}
								<p class="text-xs text-dark-500 mt-0.5">{node.instance_type}</p>
							{/if}
						</div>
						<span class="text-lg">🖥️</span>
					</div>

					<!-- Info Grid -->
					<div class="grid grid-cols-2 gap-3 mb-4">
						{#if node.architecture}
							<div>
								<p class="text-[10px] uppercase tracking-wider text-dark-600">Arch</p>
								<p class="text-xs text-dark-300 font-mono">{node.architecture}</p>
							</div>
						{/if}
						{#if node.os}
							<div>
								<p class="text-[10px] uppercase tracking-wider text-dark-600">OS</p>
								<p class="text-xs text-dark-300 font-mono">{node.os}</p>
							</div>
						{/if}
						{#if node.zone}
							<div>
								<p class="text-[10px] uppercase tracking-wider text-dark-600">Zone</p>
								<p class="text-xs text-dark-300 font-mono">{node.zone}</p>
							</div>
						{/if}
						{#if node.region}
							<div>
								<p class="text-[10px] uppercase tracking-wider text-dark-600">Region</p>
								<p class="text-xs text-dark-300 font-mono">{node.region}</p>
							</div>
						{/if}
					</div>

					<!-- Resource Bars -->
					<div class="space-y-3 pt-3 border-t border-dark-700/50">
						<!-- CPU -->
						<div>
							<div class="flex items-center justify-between mb-1">
								<span class="text-[10px] uppercase tracking-wider text-dark-600">CPU</span>
								<span class="text-xs text-dark-400">
									{formatCPU(node.cpu_allocatable_millicores)} / {formatCPU(node.cpu_capacity_millicores)}
								</span>
							</div>
							<div class="h-1.5 bg-dark-700 rounded-full overflow-hidden">
								<div
									class="h-full rounded-full transition-all duration-500
									       {cpuPercent(node) > 80 ? 'bg-red-500' : cpuPercent(node) > 60 ? 'bg-yellow-500' : 'bg-green-500'}"
									style="width: {cpuPercent(node) ?? 0}%"
								></div>
							</div>
						</div>

						<!-- Memory -->
						<div>
							<div class="flex items-center justify-between mb-1">
								<span class="text-[10px] uppercase tracking-wider text-dark-600">Memory</span>
								<span class="text-xs text-dark-400">
									{formatBytes(node.memory_allocatable_bytes)} / {formatBytes(node.memory_capacity_bytes)}
								</span>
							</div>
							<div class="h-1.5 bg-dark-700 rounded-full overflow-hidden">
								<div
									class="h-full rounded-full transition-all duration-500
									       {memPercent(node) > 80 ? 'bg-red-500' : memPercent(node) > 60 ? 'bg-yellow-500' : 'bg-blue-500'}"
									style="width: {memPercent(node) ?? 0}%"
								></div>
							</div>
						</div>
					</div>

					<!-- Boavizta data -->
					{#if node.boavizta_cpu_name || node.tdp_watts}
						<div class="mt-3 pt-3 border-t border-dark-700/50">
							<p class="text-[10px] uppercase tracking-wider text-dark-600 mb-1">Hardware Profile</p>
							<div class="flex flex-wrap gap-1">
								{#if node.boavizta_cpu_name}
									<span class="badge-green text-[10px]">{node.boavizta_cpu_name}</span>
								{/if}
								{#if node.tdp_watts}
									<span class="badge-blue text-[10px]">{node.tdp_watts}W TDP</span>
								{/if}
								{#if node.gpu_count}
									<span class="badge-purple text-[10px]">{node.gpu_count} GPU</span>
								{/if}
							</div>
						</div>
					{/if}
				</div>
			{/each}
		</div>
	</DataState>
</div>
