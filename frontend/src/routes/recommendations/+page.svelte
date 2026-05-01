<script>
	import { onMount } from 'svelte';
	import { selectedNamespace } from '$lib/stores.js';
	import {
		getActiveRecommendations,
		getIgnoredRecommendations,
		getAppliedRecommendations,
		getRecommendationSavings,
		ignoreRecommendation,
		unignoreRecommendation
	} from '$lib/api.js';
	import { formatCO2, formatCost, formatCPU, formatBytes } from '$lib/utils/format.js';
	import DataState from '$lib/components/DataState.svelte';

	// --- State ---
	let activeRecs = [];
	let ignoredRecs = [];
	let appliedRecs = [];
	let savings = null;
	let loading = true;
	let error = null;

	let activeTab = 'active'; // 'active' | 'ignored' | 'savings'
	let filterType = 'all';
	let expandedApplied = new Set(); // ids of expanded applied cards

	// Ignore modal state
	let ignoreModal = null; // { rec } | null
	let ignoreReason = '';
	let ignoreLoading = false;
	let ignoreError = null;

	// Per-card action loading state
	let actionLoading = {}; // { [id]: bool }

	$: if ($selectedNamespace !== undefined) loadData();

	async function loadData() {
		loading = true;
		error = null;
		try {
			[activeRecs, ignoredRecs, appliedRecs, savings] = await Promise.all([
				getActiveRecommendations({ namespace: $selectedNamespace || undefined, refresh: true }),
				getIgnoredRecommendations(),
				getAppliedRecommendations(),
				getRecommendationSavings()
			]);
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	// --- Type filter ---
	$: currentList = activeTab === 'active' ? activeRecs : ignoredRecs;
	$: types = [...new Set(currentList.map(r => r.type))];
	$: filtered = filterType === 'all'
		? currentList
		: currentList.filter(r => r.type === filterType);

	$: totalSavingsCO2 = activeRecs.reduce((s, r) => s + (r.potential_savings_co2e_grams ?? 0), 0);
	$: totalSavingsCost = activeRecs.reduce((s, r) => s + (r.potential_savings_cost ?? 0), 0);

	function switchTab(tab) {
		activeTab = tab;
		filterType = 'all';
	}

	// --- Ignore ---
	function openIgnoreModal(rec) {
		ignoreModal = { rec };
		ignoreReason = '';
		ignoreError = null;
	}

	function closeIgnoreModal() {
		ignoreModal = null;
		ignoreReason = '';
		ignoreError = null;
	}

	async function confirmIgnore() {
		if (!ignoreReason.trim()) {
			ignoreError = 'A reason is required.';
			return;
		}
		ignoreLoading = true;
		ignoreError = null;
		try {
			await ignoreRecommendation(ignoreModal.rec.id, { reason: ignoreReason.trim() });
			closeIgnoreModal();
			await loadData();
		} catch (e) {
			ignoreError = e.message;
		} finally {
			ignoreLoading = false;
		}
	}

	// --- Un-ignore ---
	async function handleUnignore(rec) {
		actionLoading = { ...actionLoading, [rec.id]: true };
		try {
			await unignoreRecommendation(rec.id);
			await loadData();
		} catch (e) {
			error = e.message;
		} finally {
			actionLoading = { ...actionLoading, [rec.id]: false };
		}
	}

	function toggleApplied(id) {
		expandedApplied = expandedApplied.has(id)
			? new Set([...expandedApplied].filter(x => x !== id))
			: new Set([...expandedApplied, id]);
	}

	// --- Type config ---
	const typeConfig = {
		ZOMBIE_POD:              { icon: '💀', label: 'Zombie Pod',           color: 'red',    desc: 'Pod with no meaningful activity' },
		RIGHTSIZING_CPU:         { icon: '📐', label: 'CPU Rightsizing',      color: 'yellow', desc: 'CPU request can be optimized' },
		RIGHTSIZING_MEMORY:      { icon: '📐', label: 'Memory Rightsizing',   color: 'yellow', desc: 'Memory request can be optimized' },
		AUTOSCALING_CANDIDATE:   { icon: '📈', label: 'Autoscaling',          color: 'orange', desc: 'Workload with spiky usage — consider HPA' },
		OFF_PEAK_SCALING:        { icon: '🌙', label: 'Off-Peak Scaling',     color: 'indigo', desc: 'Idle during off-peak hours — scale down with cron' },
		IDLE_NAMESPACE:          { icon: '💤', label: 'Idle Namespace',       color: 'purple', desc: 'Namespace with minimal activity' },
		CARBON_AWARE_SCHEDULING: { icon: '🌍', label: 'Carbon-Aware',         color: 'green',  desc: 'Could run in a lower-carbon zone' },
		OVERPROVISIONED_NODE:    { icon: '🖥️', label: 'Overprovisioned Node', color: 'blue',   desc: 'Node with very low utilization' },
		UNDERUTILIZED_NODE:      { icon: '🔻', label: 'Underutilized Node',   color: 'blue',   desc: 'Node with few pods — consider draining' },
	};

	function getTypeConfig(type) {
		return typeConfig[type] ?? { icon: '❓', label: type, color: 'blue', desc: '' };
	}

	onMount(() => loadData());
</script>

<!-- ─── Ignore Modal ─────────────────────────────────────────────────────── -->
{#if ignoreModal}
	<!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
		on:click|self={closeIgnoreModal}
	>
		<div class="bg-dark-900 border border-dark-700 rounded-xl shadow-2xl w-full max-w-md p-6 space-y-4">
			<div class="flex items-start justify-between gap-4">
				<div>
					<h2 class="text-base font-semibold text-dark-100">Ignore recommendation</h2>
					<p class="text-xs text-dark-500 mt-1 break-words">
						{getTypeConfig(ignoreModal.rec.type).icon}
						{ignoreModal.rec.pod_name ?? ignoreModal.rec.target_node ?? ignoreModal.rec.namespace ?? 'Cluster-wide'}
						— {getTypeConfig(ignoreModal.rec.type).label}
					</p>
				</div>
				<button
					class="text-dark-500 hover:text-dark-200 transition-colors text-xl leading-none flex-shrink-0"
					on:click={closeIgnoreModal}
				>✕</button>
			</div>

			<div class="space-y-1">
				<label class="text-xs text-dark-400 font-medium" for="ignore-reason">
					Reason <span class="text-red-400">*</span>
				</label>
				<textarea
					id="ignore-reason"
					bind:value={ignoreReason}
					placeholder="e.g. Intentional burst workload, reviewed and accepted"
					rows="3"
					class="w-full bg-dark-800 border border-dark-600 rounded-lg px-3 py-2 text-sm text-dark-100
					       placeholder-dark-600 focus:outline-none focus:border-green-600 resize-none"
				></textarea>
			</div>

			{#if ignoreError}
				<p class="text-xs text-red-400">{ignoreError}</p>
			{/if}

			<div class="flex gap-3 justify-end pt-1">
				<button class="btn-secondary text-xs" on:click={closeIgnoreModal}>Cancel</button>
				<button
					class="btn-primary text-xs flex items-center gap-2 disabled:opacity-50"
					disabled={ignoreLoading}
					on:click={confirmIgnore}
				>
					{#if ignoreLoading}<span class="animate-spin">⟳</span>{/if}
					Confirm ignore
				</button>
			</div>
		</div>
	</div>
{/if}

<!-- ─── Page ─────────────────────────────────────────────────────────────── -->
<div class="p-6 lg:p-8 space-y-6 max-w-[1600px] mx-auto">
	<!-- Header -->
	<div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Recommendations</h1>
			<p class="text-sm text-dark-500 mt-1">Actionable suggestions to reduce your cluster's environmental footprint</p>
		</div>
		<div class="flex items-center gap-3">
			{#if $selectedNamespace}
				<button class="btn-secondary text-xs" on:click={() => selectedNamespace.set('')}>
					Clear filter: {$selectedNamespace} ✕
				</button>
			{/if}
			<button class="btn-secondary text-xs" on:click={loadData}>↻ Refresh</button>
		</div>
	</div>

	<!-- Tabs -->
	<div class="flex gap-1 bg-dark-900 rounded-xl p-1 w-fit border border-dark-700">
		<button
			class="px-4 py-2 rounded-lg text-sm font-medium transition-colors
			       {activeTab === 'active' ? 'bg-dark-700 text-dark-100' : 'text-dark-500 hover:text-dark-300'}"
			on:click={() => switchTab('active')}
		>
			Active
			<span class="ml-1.5 text-xs px-1.5 py-0.5 rounded-full
			             {activeTab === 'active' ? 'bg-green-600/20 text-green-400' : 'bg-dark-700 text-dark-500'}">
				{activeRecs.length}
			</span>
		</button>
		<button
			class="px-4 py-2 rounded-lg text-sm font-medium transition-colors
			       {activeTab === 'ignored' ? 'bg-dark-700 text-dark-100' : 'text-dark-500 hover:text-dark-300'}"
			on:click={() => switchTab('ignored')}
		>
			Ignored
			<span class="ml-1.5 text-xs px-1.5 py-0.5 rounded-full
			             {activeTab === 'ignored' ? 'bg-yellow-600/20 text-yellow-400' : 'bg-dark-700 text-dark-500'}">
				{ignoredRecs.length}
			</span>
		</button>
		<button
			class="px-4 py-2 rounded-lg text-sm font-medium transition-colors
			       {activeTab === 'savings' ? 'bg-dark-700 text-dark-100' : 'text-dark-500 hover:text-dark-300'}"
			on:click={() => switchTab('savings')}
		>
			💰 Realized Savings
			{#if savings?.applied_count}
				<span class="ml-1.5 text-xs px-1.5 py-0.5 rounded-full bg-blue-600/20 text-blue-400">
					{savings.applied_count}
				</span>
			{/if}
		</button>
	</div>

	<DataState {loading} {error}>
		<!-- ═══ SAVINGS TAB ═══════════════════════════════════════════════════════ -->
		{#if activeTab === 'savings'}
			{#if savings}
				<div class="space-y-6">
					<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
						<div class="card-compact text-center">
							<p class="stat-label">Applied Recommendations</p>
							<p class="stat-value text-2xl text-blue-400">{savings.applied_count}</p>
						</div>
						<div class="card-compact text-center">
							<p class="stat-label">CO₂ Avoided</p>
							<p class="stat-value text-2xl text-green-400">{formatCO2(savings.total_carbon_saved_co2e_grams)}</p>
						</div>
						<div class="card-compact text-center">
							<p class="stat-label">Cost Saved</p>
							<p class="stat-value text-2xl text-blue-400">{formatCost(savings.total_cost_saved)}</p>
						</div>
					</div>

					{#if appliedRecs.length}
						<div class="space-y-2">
							<h2 class="text-sm font-semibold text-dark-300">Applied Recommendations</h2>
							{#each appliedRecs as rec (rec.id)}
								{@const cfg = getTypeConfig(rec.type)}
								{@const expanded = expandedApplied.has(rec.id)}
								<!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
								<div
									class="card cursor-pointer hover:border-blue-600/30 transition-all duration-200 select-none"
									on:click={() => toggleApplied(rec.id)}
								>
									<!-- Summary row (always visible) -->
									<div class="flex items-center gap-4">
										<div class="text-xl flex-shrink-0">{cfg.icon}</div>
										<div class="flex-1 min-w-0">
											<div class="flex items-center gap-2 flex-wrap">
												<span class="text-sm font-semibold text-dark-100">
													{rec.pod_name ?? rec.target_node ?? rec.namespace ?? 'Cluster-wide'}
												</span>
												<span class="badge-{cfg.color} text-[10px]">{cfg.label}</span>
												<span class="text-[10px] px-2 py-0.5 rounded bg-blue-600/10 text-blue-400">applied</span>
											</div>
											{#if rec.namespace}
												<p class="text-xs text-dark-500 mt-0.5">Namespace: <span class="text-dark-400">{rec.namespace}</span></p>
											{/if}
										</div>
										<!-- Savings chips -->
										<div class="flex items-center gap-3 flex-shrink-0">
											{#if rec.carbon_saved_co2e_grams}
												<span class="text-xs font-semibold text-green-400">-{formatCO2(rec.carbon_saved_co2e_grams)}</span>
											{/if}
											{#if rec.cost_saved}
												<span class="text-xs text-blue-400">-{formatCost(rec.cost_saved)}</span>
											{/if}
											<span class="text-dark-500 text-xs transition-transform duration-200 {expanded ? 'rotate-180' : ''}">▼</span>
										</div>
									</div>

									<!-- Expanded detail -->
									{#if expanded}
										<div class="mt-4 pt-4 border-t border-dark-700 space-y-3" on:click|stopPropagation>
											<p class="text-sm text-dark-400">{rec.reason}</p>

											<div class="grid grid-cols-2 sm:grid-cols-3 gap-3">
												{#if rec.applied_at}
													<div>
														<p class="text-[10px] uppercase text-dark-600">Applied on</p>
														<p class="text-xs text-dark-300">{new Date(rec.applied_at).toLocaleDateString()}</p>
													</div>
												{/if}
												{#if rec.carbon_saved_co2e_grams}
													<div>
														<p class="text-[10px] uppercase text-dark-600">CO₂ Avoided</p>
														<p class="text-xs text-green-400 font-semibold">{formatCO2(rec.carbon_saved_co2e_grams)}</p>
													</div>
												{/if}
												{#if rec.cost_saved}
													<div>
														<p class="text-[10px] uppercase text-dark-600">Cost Saved</p>
														<p class="text-xs text-blue-400 font-semibold">{formatCost(rec.cost_saved)}</p>
													</div>
												{/if}
											</div>

											{#if rec.current_cpu_request_millicores != null || rec.current_memory_request_bytes != null}
												<div class="flex flex-wrap gap-4">
													{#if rec.current_cpu_request_millicores != null}
														<div class="flex items-center gap-2">
															<span class="text-[10px] uppercase text-dark-600">CPU</span>
															<span class="text-xs text-dark-400 font-mono">{formatCPU(rec.current_cpu_request_millicores)}</span>
															{#if rec.actual_cpu_request_millicores != null}
																<span class="text-dark-600">→</span>
																<span class="text-xs text-blue-400 font-mono">{formatCPU(rec.actual_cpu_request_millicores)}</span>
																<span class="text-[10px] text-dark-600">(actual)</span>
															{:else if rec.recommended_cpu_request_millicores != null}
																<span class="text-dark-600">→</span>
																<span class="text-xs text-green-400 font-mono">{formatCPU(rec.recommended_cpu_request_millicores)}</span>
																<span class="text-[10px] text-dark-600">(recommended)</span>
															{/if}
														</div>
													{/if}
													{#if rec.current_memory_request_bytes != null}
														<div class="flex items-center gap-2">
															<span class="text-[10px] uppercase text-dark-600">Memory</span>
															<span class="text-xs text-dark-400 font-mono">{formatBytes(rec.current_memory_request_bytes)}</span>
															{#if rec.actual_memory_request_bytes != null}
																<span class="text-dark-600">→</span>
																<span class="text-xs text-blue-400 font-mono">{formatBytes(rec.actual_memory_request_bytes)}</span>
																<span class="text-[10px] text-dark-600">(actual)</span>
															{:else if rec.recommended_memory_request_bytes != null}
																<span class="text-dark-600">→</span>
																<span class="text-xs text-green-400 font-mono">{formatBytes(rec.recommended_memory_request_bytes)}</span>
																<span class="text-[10px] text-dark-600">(recommended)</span>
															{/if}
														</div>
													{/if}
												</div>
											{/if}

											{#if rec.target_node}
												<div class="flex items-center gap-2">
													<span class="text-[10px] uppercase text-dark-600">Node</span>
													<span class="text-xs text-dark-300 font-mono">{rec.target_node}</span>
												</div>
											{/if}

											<p class="text-[10px] text-dark-600 italic">Recorded on {new Date(rec.created_at).toLocaleDateString()}</p>
										</div>
									{/if}
								</div>
							{/each}
						</div>
					{:else}
						<div class="card text-center py-12">
							<p class="text-4xl mb-3">🌱</p>
							<p class="text-dark-400 text-sm">No applied recommendations yet.</p>
							<p class="text-dark-600 text-xs mt-1">Mark active recommendations as applied via the API to track your impact here.</p>
						</div>
					{/if}
				</div>
			{/if}

		<!-- ═══ ACTIVE / IGNORED TABS ════════════════════════════════════════════ -->
		{:else}
			<!-- Potential savings summary (active only) -->
			{#if activeTab === 'active' && activeRecs.length}
				<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
					<div class="card-compact text-center">
						<p class="stat-label">Active Recommendations</p>
						<p class="stat-value text-2xl">{activeRecs.length}</p>
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
			{/if}

			{#if activeTab === 'ignored' && ignoredRecs.length === 0}
				<div class="card text-center py-12">
					<p class="text-4xl mb-3">👀</p>
					<p class="text-dark-400 text-sm">No ignored recommendations.</p>
				</div>
			{:else if activeTab === 'active' && activeRecs.length === 0}
				<div class="card text-center py-12">
					<p class="text-4xl mb-3">🎉</p>
					<p class="text-dark-400 text-sm">No active recommendations — your cluster looks great!</p>
				</div>
			{:else}
				<!-- Type filter tabs -->
				{#if types.length > 1}
					<div class="flex flex-wrap gap-2">
						<button
							class="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors
							       {filterType === 'all' ? 'bg-green-600/20 text-green-400' : 'bg-dark-800 text-dark-400 hover:text-dark-200'}"
							on:click={() => filterType = 'all'}
						>
							All ({currentList.length})
						</button>
						{#each types as type}
							{@const cfg = getTypeConfig(type)}
							{@const count = currentList.filter(r => r.type === type).length}
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
					{#each filtered as rec (rec.id)}
						{@const cfg = getTypeConfig(rec.type)}
						<div class="card hover:border-{cfg.color}-600/30 transition-all duration-200">
							<div class="flex items-start gap-4">
								<div class="text-2xl flex-shrink-0 mt-0.5">{cfg.icon}</div>
								<div class="flex-1 min-w-0">
									<!-- Top row: title + actions -->
									<div class="flex items-start justify-between gap-3 flex-wrap">
										<div class="min-w-0">
											<div class="flex items-center gap-2 flex-wrap">
												<h3 class="text-sm font-semibold text-dark-100">
													{rec.pod_name ?? rec.target_node ?? rec.namespace ?? 'Cluster-wide'}
												</h3>
												<span class="badge-{cfg.color} text-[10px]">{cfg.label}</span>
												{#if activeTab === 'ignored'}
													<span class="text-[10px] px-2 py-0.5 rounded bg-yellow-600/10 text-yellow-500">ignored</span>
												{/if}
											</div>
											{#if rec.namespace}
												<p class="text-xs text-dark-500 mt-0.5">Namespace: <span class="text-dark-400">{rec.namespace}</span></p>
											{/if}
										</div>
										<div class="flex items-center gap-3 flex-shrink-0">
											{#if rec.potential_savings_co2e_grams || rec.potential_savings_cost}
												<div class="text-right">
													{#if rec.potential_savings_co2e_grams}
														<p class="text-sm font-bold text-green-400">-{formatCO2(rec.potential_savings_co2e_grams)}</p>
													{/if}
													{#if rec.potential_savings_cost}
														<p class="text-xs text-blue-400">-{formatCost(rec.potential_savings_cost)}</p>
													{/if}
												</div>
											{/if}
											{#if activeTab === 'active'}
												<button
													class="text-xs px-3 py-1.5 rounded-lg border border-dark-600 text-dark-400
													       hover:border-yellow-600/50 hover:text-yellow-400 transition-colors"
													title="Ignore this recommendation"
													on:click={() => openIgnoreModal(rec)}
												>Ignore</button>
											{:else}
												<button
													class="text-xs px-3 py-1.5 rounded-lg border border-dark-600 text-dark-400
													       hover:border-green-600/50 hover:text-green-400 transition-colors disabled:opacity-40"
													disabled={actionLoading[rec.id]}
													title="Restore to active"
													on:click={() => handleUnignore(rec)}
												>
													{#if actionLoading[rec.id]}<span class="animate-spin inline-block">⟳</span>{:else}↩ Restore{/if}
												</button>
											{/if}
										</div>
									</div>

									<!-- Ignored reason banner -->
									{#if activeTab === 'ignored' && rec.ignored_reason}
										<div class="mt-2 text-xs text-yellow-400/80 bg-yellow-600/5 border border-yellow-600/20 rounded-lg px-3 py-2">
											<span class="text-dark-600 uppercase text-[10px] mr-1">Reason:</span>{rec.ignored_reason}
										</div>
									{/if}

									<!-- Description / reason -->
									<p class="text-sm text-dark-400 mt-2">{rec.reason}</p>

									<!-- CPU / Memory details -->
									{#if rec.current_cpu_request_millicores != null || rec.current_memory_request_bytes != null}
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

									<!-- Cron schedule -->
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

									<!-- Footer: priority + date -->
									<div class="mt-3 flex items-center gap-3 flex-wrap">
										{#if rec.priority}
											<span class="text-[10px] uppercase px-2 py-0.5 rounded
												{rec.priority === 'high'   ? 'bg-red-600/20 text-red-400' :
												 rec.priority === 'medium' ? 'bg-yellow-600/20 text-yellow-400' :
												                             'bg-dark-700 text-dark-400'}">
												{rec.priority} priority
											</span>
										{/if}
										{#if activeTab === 'ignored' && rec.ignored_at}
											<span class="text-[10px] text-dark-600">
												Ignored on {new Date(rec.ignored_at).toLocaleDateString()}
											</span>
										{/if}
									</div>
								</div>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		{/if}
	</DataState>
</div>
