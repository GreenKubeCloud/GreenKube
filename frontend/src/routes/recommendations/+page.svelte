<script>
	import { onMount } from 'svelte';
	import { selectedNamespace } from '$lib/stores.js';
	import {
		getActiveRecommendations,
		getIgnoredRecommendations,
		getAppliedRecommendations,
		getRecommendationSavings,
		getTopRecommendations,
		ignoreRecommendation,
		unignoreRecommendation,
		applyRecommendationPr,
		getRecommendationEvents
	} from '$lib/api.js';
	import { formatCO2, formatCost, formatCPU, formatBytes } from '$lib/utils/format.js';
	import { groupConsecutiveEvents } from '$lib/utils/lifecycle.js';
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
	let expandedEvidence = new Set(); // ids of active cards showing the evidence panel

	// Multi-criteria ranking
	let rankingProfile = ''; // '' = server default (projected savings)
	let rankedIds = []; // recommendation ids ordered by the selected profile

	const RANKING_PROFILES = [
		{ value: '', label: 'Projected savings' },
		{ value: 'balanced', label: 'Balanced' },
		{ value: 'carbon_first', label: 'Carbon first' },
		{ value: 'cost_first', label: 'Cost first' },
		{ value: 'quick_wins', label: 'Quick wins' },
		{ value: 'low_risk', label: 'Low risk' }
	];

	// Ignore modal state
	let ignoreModal = null; // { rec } | null
	let ignoreReason = '';
	let ignoreLoading = false;
	let ignoreError = null;

	// Create-PR modal state (Phase 4)
	let prModal = null; // { rec } | null
	let prBaseBranch = '';
	let prDiff = null;
	let prResult = null;
	let prLoading = false;
	let prError = null;

	// Verification event trail per applied recommendation
	let eventsByRec = {}; // { [id]: Event[] }

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

	$: displayed = rankedIds.length
		? [...filtered].sort((a, b) => {
			const ia = rankedIds.indexOf(a.id);
			const ib = rankedIds.indexOf(b.id);
			return (ia === -1 ? Number.MAX_SAFE_INTEGER : ia) - (ib === -1 ? Number.MAX_SAFE_INTEGER : ib);
		})
		: filtered;

	async function applyRankingProfile(profile) {
		rankingProfile = profile;
		rankedIds = [];
		if (!profile) return;
		try {
			const ranked = await getTopRecommendations({
				namespace: $selectedNamespace || undefined,
				limit: 50,
				metric: 'co2',
				profile
			});
			rankedIds = ranked.map((r) => r.id).filter((id) => id != null);
		} catch (e) {
			error = e.message;
		}
	}

	$: totalSavingsCO2 = activeRecs.reduce((s, r) => s + (r.potential_savings_co2e_grams ?? 0), 0);
	$: totalSavingsCost = activeRecs.reduce((s, r) => s + (r.potential_savings_cost ?? 0), 0);
	$: potentialSavingsPeriod = 'per year';

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
		ignoreLoading = true;
		ignoreError = null;
		try {
			await ignoreRecommendation(ignoreModal.rec.id, { reason: ignoreReason.trim() || null });
			closeIgnoreModal();
			await loadData();
		} catch (e) {
			ignoreError = e.message;
		} finally {
			ignoreLoading = false;
		}
	}

	// --- Create PR ---
	const RIGHTSIZING_TYPES = ['RIGHTSIZING_CPU', 'RIGHTSIZING_MEMORY'];

	function canCreatePr(rec) {
		return RIGHTSIZING_TYPES.includes(rec.type) && (rec.owner_kind || rec.pod_name);
	}

	async function openPrModal(rec) {
		prModal = { rec };
		prBaseBranch = '';
		prDiff = null;
		prResult = null;
		prError = null;
		prLoading = true;
		try {
			const preview = await applyRecommendationPr(rec.id, { dry_run: true });
			if (preview.status === 'error') {
				prError = preview.message ?? 'Could not prepare the pull request.';
			} else {
				prDiff = preview.diff;
				prBaseBranch = preview.base_branch ?? '';
			}
		} catch (e) {
			prError = e.message;
		} finally {
			prLoading = false;
		}
	}

	function closePrModal() {
		prModal = null;
		prDiff = null;
		prResult = null;
		prError = null;
	}

	async function confirmPr() {
		prLoading = true;
		prError = null;
		try {
			const result = await applyRecommendationPr(prModal.rec.id, {
				dry_run: false,
				base_branch: prBaseBranch || undefined
			});
			prResult = result;
			if (result.status === 'error') prError = result.message;
			if (result.status === 'pr_open') await loadData();
		} catch (e) {
			prError = e.message;
		} finally {
			prLoading = false;
		}
	}

	// --- Verification events ---
	async function toggleEvents(rec) {
		if (eventsByRec[rec.id]) {
			const copy = { ...eventsByRec };
			delete copy[rec.id];
			eventsByRec = copy;
			return;
		}
		try {
			const events = await getRecommendationEvents(rec.id);
			eventsByRec = { ...eventsByRec, [rec.id]: events };
		} catch (e) {
			error = e.message;
		}
	}

	function verificationClass(status) {
		if (status === 'passed') return 'bg-green-600/20 text-green-400';
		if (status === 'failed') return 'bg-red-600/20 text-red-400';
		if (status === 'inconclusive') return 'bg-yellow-600/20 text-yellow-400';
		return 'bg-blue-600/10 text-blue-400';
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

	function toggleEvidence(id) {
		expandedEvidence = expandedEvidence.has(id)
			? new Set([...expandedEvidence].filter(x => x !== id))
			: new Set([...expandedEvidence, id]);
	}

	// --- Source / risk display ---
	const sourceConfig = {
		greenkube: { label: 'GreenKube', color: 'green' },
		vpa: { label: 'VPA', color: 'blue' },
		karpenter: { label: 'Karpenter', color: 'purple' }
	};

	function getSourceConfig(source) {
		return sourceConfig[source] ?? { label: source ?? 'GreenKube', color: 'green' };
	}

	function riskClass(risk) {
		if (risk === 'high') return 'bg-red-600/20 text-red-400';
		if (risk === 'medium') return 'bg-yellow-600/20 text-yellow-400';
		return 'bg-green-600/20 text-green-400';
	}

	function percent(value) {
		if (value == null) return '—';
		return `${Math.round(value * 100)}%`;
	}

	function rollbackLabel(condition) {
		return `${condition.metric} ${condition.comparator} ${condition.threshold} — ${condition.description}`;
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
		ORPHANED_PERSISTENT_VOLUME: { icon: '💾', label: 'Orphaned PV',      color: 'orange', desc: 'PersistentVolume with no bound claim' },
		ORPHANED_LOAD_BALANCER:    { icon: '🌐', label: 'Orphaned LoadBalancer', color: 'orange', desc: 'LoadBalancer Service with no backing endpoints' },
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
					Reason <span class="text-dark-500">(optional)</span>
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

<!-- ─── Create PR Modal ──────────────────────────────────────────────────── -->
{#if prModal}
	<!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
		on:click|self={closePrModal}
	>
		<div class="bg-dark-900 border border-dark-700 rounded-xl shadow-2xl w-full max-w-2xl p-6 space-y-4 max-h-[85vh] overflow-y-auto">
			<div class="flex items-start justify-between gap-4">
				<div>
					<h2 class="text-base font-semibold text-dark-100">Apply via pull request</h2>
					<p class="text-xs text-dark-500 mt-1 break-words">
						{getTypeConfig(prModal.rec.type).icon}
						{prModal.rec.owner_kind ? `${prModal.rec.owner_kind}/${prModal.rec.owner_name}` : prModal.rec.pod_name}
						— {getTypeConfig(prModal.rec.type).label}
					</p>
				</div>
				<button
					class="text-dark-500 hover:text-dark-200 transition-colors text-xl leading-none flex-shrink-0"
					on:click={closePrModal}
				>✕</button>
			</div>

			{#if prLoading && !prDiff && !prResult}
				<p class="text-xs text-dark-400 flex items-center gap-2"><span class="animate-spin">⟳</span> Preparing preview…</p>
			{/if}

			{#if prError}
				<div class="text-xs text-red-400 bg-red-600/10 border border-red-600/30 rounded-lg px-3 py-2">
					{prError}
				</div>
			{/if}

			{#if prResult?.status === 'pr_open'}
				<div class="text-xs text-green-400 bg-green-600/10 border border-green-600/30 rounded-lg px-3 py-2">
					Pull request opened:
					<a class="underline" href={prResult.pr_url} target="_blank" rel="noreferrer">{prResult.pr_url}</a>
				</div>
			{/if}

			{#if prDiff}
				<div class="space-y-1">
					<p class="text-xs text-dark-400 font-medium">Proposed diff</p>
					<pre class="text-[11px] leading-relaxed text-dark-300 bg-dark-950 border border-dark-700 rounded-lg p-3 overflow-x-auto whitespace-pre">{prDiff}</pre>
				</div>

				<div class="space-y-1">
					<label class="text-xs text-dark-400 font-medium" for="pr-base-branch">Base branch</label>
					<input
						id="pr-base-branch"
						bind:value={prBaseBranch}
						placeholder="main"
						class="w-full bg-dark-800 border border-dark-600 rounded-lg px-3 py-2 text-sm text-dark-100
						       placeholder-dark-600 focus:outline-none focus:border-green-600"
					/>
				</div>
			{/if}

			<div class="flex gap-3 justify-end pt-1">
				<button class="btn-secondary text-xs" on:click={closePrModal}>Close</button>
				{#if !prResult && prDiff}
					<button
						class="btn-primary text-xs flex items-center gap-2 disabled:opacity-50"
						disabled={prLoading}
						on:click={confirmPr}
					>
						{#if prLoading}<span class="animate-spin">⟳</span>{/if}
						Create pull request
					</button>
				{/if}
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
			<select
				class="bg-dark-800 border border-dark-600 rounded-lg px-2 py-1.5 text-xs text-dark-200"
				bind:value={rankingProfile}
				on:change={(e) => applyRankingProfile(e.currentTarget.value)}
				title="Multi-criteria ranking profile"
			>
				{#each RANKING_PROFILES as profile}
					<option value={profile.value}>{profile.label}</option>
				{/each}
			</select>
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
							{#if savings.verified_count != null}
								<p class="text-[10px] text-dark-600 uppercase tracking-wide mt-1">{savings.verified_count} verified</p>
							{/if}
						</div>
						<div class="card-compact text-center">
							<p class="stat-label">CO₂ Avoided</p>
							<p class="stat-value text-2xl text-green-400">{formatCO2(savings.total_carbon_saved_co2e_grams)}</p>
							<p class="text-[10px] text-dark-600 uppercase tracking-wide mt-1">
								measured {formatCO2(savings.measured_carbon_saved_co2e_grams ?? 0)} ·
								projected {formatCO2(savings.prorated_carbon_saved_co2e_grams ?? 0)}
							</p>
						</div>
						<div class="card-compact text-center">
							<p class="stat-label">Cost Saved</p>
							<p class="stat-value text-2xl text-blue-400">{formatCost(savings.total_cost_saved)}</p>
							<p class="text-[10px] text-dark-600 uppercase tracking-wide mt-1">
								measured {formatCost(savings.measured_cost_saved ?? 0)} ·
								projected {formatCost(savings.prorated_cost_saved ?? 0)}
							</p>
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
												{#if rec.status === 'verified'}
													<span class="text-[10px] px-2 py-0.5 rounded bg-green-600/20 text-green-400">✓ verified</span>
												{:else if rec.status === 'rollback_review'}
													<span class="text-[10px] px-2 py-0.5 rounded bg-red-600/20 text-red-400">rollback review</span>
												{:else if rec.status === 'verifying'}
													<span class="text-[10px] px-2 py-0.5 rounded bg-blue-600/20 text-blue-400">verifying…</span>
												{:else}
													<span class="text-[10px] px-2 py-0.5 rounded bg-blue-600/10 text-blue-400">applied</span>
												{/if}
												{#if rec.verification_status}
													<span class="text-[10px] px-2 py-0.5 rounded {verificationClass(rec.verification_status)}">
														{rec.verification_status}
													</span>
												{/if}
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
														<p class="text-[10px] uppercase text-dark-600">
															{rec.status === 'verified' && rec.measured_co2e_saved_grams != null ? 'CO₂ Measured' : 'CO₂ Projected'}
														</p>
														<p class="text-xs text-green-400 font-semibold">
															{formatCO2(rec.status === 'verified' && rec.measured_co2e_saved_grams != null ? rec.measured_co2e_saved_grams : rec.carbon_saved_co2e_grams)}
														</p>
													</div>
												{/if}
												{#if rec.cost_saved}
													<div>
														<p class="text-[10px] uppercase text-dark-600">
															{rec.status === 'verified' && rec.measured_cost_saved != null ? 'Cost Measured' : 'Cost Projected'}
														</p>
														<p class="text-xs text-blue-400 font-semibold">
															{formatCost(rec.status === 'verified' && rec.measured_cost_saved != null ? rec.measured_cost_saved : rec.cost_saved)}
														</p>
													</div>
												{/if}
											</div>

											<div class="flex flex-wrap items-center gap-3">
												{#if rec.application_method}
													<div class="flex items-center gap-2">
														<span class="text-[10px] uppercase text-dark-600">Applied via</span>
														<span class="text-xs text-dark-300">{rec.application_method}</span>
													</div>
												{/if}
												{#if rec.verified_at}
													<div class="flex items-center gap-2">
														<span class="text-[10px] uppercase text-dark-600">Verified on</span>
														<span class="text-xs text-dark-300">{new Date(rec.verified_at).toLocaleDateString()}</span>
													</div>
												{/if}
												<button
													class="text-[10px] px-2 py-0.5 rounded border border-dark-600 text-dark-400
													       hover:border-blue-600/50 hover:text-blue-400 transition-colors"
													on:click|stopPropagation={() => toggleEvents(rec)}
												>
													{eventsByRec[rec.id] ? 'Hide lifecycle' : 'Lifecycle'}
												</button>
											</div>

											{#if eventsByRec[rec.id]}
												<div class="space-y-1 border-t border-dark-700 pt-2">
													{#each groupConsecutiveEvents(eventsByRec[rec.id]) as event}
														<p class="text-[11px] text-dark-400">
															<span class="text-dark-600">
																{new Date(event.first.created_at).toLocaleString()}{#if event.count > 1} → {new Date(event.last.created_at).toLocaleString()}{/if}
															</span>
															• <span class="text-dark-200">{event.event_type}</span>
															{#if event.count > 1}
																<span class="text-dark-600">×{event.count}</span>
															{/if}
															<span class="text-dark-600">by {event.actor}</span>
														</p>
													{/each}
												</div>
											{/if}

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
						<p class="text-[10px] text-dark-600 uppercase tracking-wide mt-1">{potentialSavingsPeriod}</p>
					</div>
					<div class="card-compact text-center">
						<p class="stat-label">Potential Cost Savings</p>
						<p class="stat-value text-2xl text-blue-400">{formatCost(totalSavingsCost)}</p>
						<p class="text-[10px] text-dark-600 uppercase tracking-wide mt-1">{potentialSavingsPeriod}</p>
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
					{#each displayed as rec (rec.id)}
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
												<span class="text-[10px] px-2 py-0.5 rounded bg-dark-700 text-dark-300">
													{getSourceConfig(rec.source).label}
												</span>
												{#if rec.risk_level}
													<span class="text-[10px] px-2 py-0.5 rounded {riskClass(rec.risk_level)}"
														title={rec.risk_factors?.join(', ')}>
														{rec.risk_level} risk
													</span>
												{/if}
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
												{#if canCreatePr(rec)}
													<button
														class="text-xs px-3 py-1.5 rounded-lg border border-dark-600 text-dark-400
														       hover:border-green-600/50 hover:text-green-400 transition-colors"
														title="Open a pull request that applies this recommendation"
														on:click={() => openPrModal(rec)}
													>Create PR</button>
												{/if}
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
										{#if rec.confidence != null}
											<span class="text-[10px] text-dark-400">
												confidence {percent(rec.confidence)}
											</span>
										{/if}
										{#if rec.ranking_score != null}
											<span class="text-[10px] text-dark-500" title={JSON.stringify(rec.ranking_factors ?? {})}>
												rank score {rec.ranking_score.toFixed(2)}
											</span>
										{/if}
										{#if rec.evidence}
											<button
												class="text-[10px] px-2 py-0.5 rounded border border-dark-600 text-dark-400
												       hover:border-green-600/50 hover:text-green-400 transition-colors"
												on:click={() => toggleEvidence(rec.id)}
											>
												{expandedEvidence.has(rec.id) ? 'Hide evidence' : 'Evidence'}
											</button>
										{/if}
										{#if activeTab === 'ignored' && rec.ignored_at}
											<span class="text-[10px] text-dark-600">
												Ignored on {new Date(rec.ignored_at).toLocaleDateString()}
											</span>
										{/if}
									</div>

									<!-- Evidence panel -->
									{#if expandedEvidence.has(rec.id) && rec.evidence}
										{@const ev = rec.evidence}
										<div class="mt-3 pt-3 border-t border-dark-700 space-y-3 text-xs">
											<div class="flex flex-wrap gap-4 text-dark-500">
												{#if ev.observation_window_start}
													<span>Window: <span class="text-dark-300">{new Date(ev.observation_window_start).toLocaleDateString()} → {new Date(ev.observation_window_end).toLocaleDateString()}</span></span>
												{/if}
												<span>Samples: <span class="text-dark-300">{ev.sample_count}</span></span>
												<span>Coverage: <span class="text-dark-300">{percent(ev.coverage_ratio)}</span></span>
												{#if ev.expires_at}
													<span>Expires: <span class="text-dark-300">{new Date(ev.expires_at).toLocaleDateString()}</span></span>
												{/if}
											</div>

											{#if ev.cpu_usage || ev.memory_usage}
												<div class="overflow-x-auto">
													<table class="text-[11px] text-dark-400">
														<thead>
															<tr class="text-dark-600 uppercase text-[10px]">
																<th class="text-left pr-4">Signal</th>
																<th class="text-right pr-4">avg</th>
																<th class="text-right pr-4">p50</th>
																<th class="text-right pr-4">p95</th>
																<th class="text-right pr-4">p99</th>
																<th class="text-right">max</th>
															</tr>
														</thead>
														<tbody>
															{#if ev.cpu_usage}
																<tr>
																	<td class="pr-4">CPU (m)</td>
																	<td class="text-right pr-4">{ev.cpu_usage.avg.toFixed(0)}</td>
																	<td class="text-right pr-4">{ev.cpu_usage.p50.toFixed(0)}</td>
																	<td class="text-right pr-4">{ev.cpu_usage.p95.toFixed(0)}</td>
																	<td class="text-right pr-4">{ev.cpu_usage.p99.toFixed(0)}</td>
																	<td class="text-right">{ev.cpu_usage.max.toFixed(0)}</td>
																</tr>
															{/if}
															{#if ev.memory_usage}
																<tr>
																	<td class="pr-4">Memory</td>
																	<td class="text-right pr-4">{formatBytes(ev.memory_usage.avg)}</td>
																	<td class="text-right pr-4">{formatBytes(ev.memory_usage.p50)}</td>
																	<td class="text-right pr-4">{formatBytes(ev.memory_usage.p95)}</td>
																	<td class="text-right pr-4">{formatBytes(ev.memory_usage.p99)}</td>
																	<td class="text-right">{formatBytes(ev.memory_usage.max)}</td>
																</tr>
															{/if}
														</tbody>
													</table>
												</div>
											{/if}

											{#if ev.changes?.length}
												<div class="flex flex-wrap gap-4 text-dark-500">
													{#each ev.changes as change}
														<span>
															{change.resource}: <span class="text-dark-300">{change.current}</span>
															→ <span class="text-green-400">{change.proposed}</span>
															{#if change.change_ratio != null}({percent(change.change_ratio)} reduction){/if}
														</span>
													{/each}
												</div>
											{/if}

											<div class="text-dark-500">
												Savings method: <span class="text-dark-300">{ev.savings_method}</span>
											</div>

											{#if ev.rollback_conditions?.length}
												<div class="space-y-1">
													<p class="text-dark-600 uppercase text-[10px]">Rollback conditions</p>
													{#each ev.rollback_conditions as condition}
														<p class="text-dark-400">• {rollbackLabel(condition)}</p>
													{/each}
												</div>
											{/if}
										</div>
									{/if}
								</div>
							</div>
						</div>
					{/each}
				</div>
			{/if}
		{/if}
	</DataState>
</div>
