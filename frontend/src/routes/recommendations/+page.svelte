<script>
	import { currentUser, demoMode, hasScope, selectedNamespace } from '$lib/stores.js';
	import { onDestroy, onMount } from 'svelte';
	import {
		getActiveRecommendations,
		getIgnoredRecommendations,
		getAppliedRecommendations,
		getRecommendationSavings,
		getOpenPullRequests,
		getAutomationStatus,
		getNamespaces,
		ignoreRecommendation,
		unignoreRecommendation,
		createAutomationPlan,
		getAutomationPlan,
		approveAutomationPlan,
		cancelAutomationPlan,
		discardAutomationPlan,
		previewAutomationPlanRollback,
		reportPlanDelivery,
		planPatchUrl,
		getRecommendationEvents
	} from '$lib/api.js';
	import {
		findPendingOptimizationRun,
		isRunPending,
		startOptimizationScan,
		waitForOptimizationRun
	} from '$lib/optimizationRuns.js';
	import { formatCO2, formatCost, formatCPU, formatBytes } from '$lib/utils/format.js';
	import { groupConsecutiveEvents } from '$lib/utils/lifecycle.js';
	import {
		describeRankingFactors,
		formatRankingScore,
		rankingFactorLabel,
		topRankingFactor
	} from '$lib/utils/ranking.js';
	import {
		describeExpectedValue,
		eligibilityOf,
		formatMoney,
		isReliabilityLane,
		oomHistoryWarningOf,
		planTargetFor
	} from '$lib/utils/decision.js';
	import DataState from '$lib/components/DataState.svelte';
	import OomHistoryWarning from '$lib/components/OomHistoryWarning.svelte';
	import { capacitySummary } from '$lib/releasedCapacity.js';
	import { etaFrom, OPPORTUNITY_LABEL, serverOpportunity } from '$lib/overview.js';
	import { SAVINGS_TERMS, realizationText, savingPhrase, savingsFigures, visibleDiagnostics } from '$lib/savingsVocabulary.js';

	// --- State ---
	let activeRecs = [];
	let ignoredRecs = [];
	let appliedRecs = [];
	let openPullRequests = [];
	let openPullRequestKeys = new Set();
	let automationStatus = null;
	let automationStatusError = '';
	let savings = null;
	let loading = true;
	let error = null;

	let activeTab = 'active'; // 'active' | 'ignored' | 'savings'
	// Mutating actions need the write scope and approvals the approve scope:
	// a viewer sees the recommendations without buttons that would end in 403.
	$: canWrite = hasScope($currentUser, 'write');
	$: canApprove = hasScope($currentUser, 'approve');
	let filterType = 'all';
	let expandedApplied = new Set(); // ids of expanded applied cards
	let expandedEvidence = new Set(); // ids of active cards showing the evidence panel

	// Ordering. 'server' keeps the API order (risk-adjusted expected value,
	// I1); the savings sorts are client-side; 'profile:*' reloads the list with
	// ?profile= so the server re-scores and re-orders every match.
	let sortMode = 'server';

	const SORT_OPTIONS = [
		{ value: 'server', label: 'Expected value (default)' },
		{ value: 'cost', label: 'Projected cost savings' },
		{ value: 'co2', label: 'Projected CO₂e savings' },
		{ value: 'profile:balanced', label: 'Profile: balanced' },
		{ value: 'profile:carbon_first', label: 'Profile: carbon first' },
		{ value: 'profile:cost_first', label: 'Profile: cost first' },
		{ value: 'profile:quick_wins', label: 'Profile: quick wins' },
		{ value: 'profile:low_risk', label: 'Profile: low risk' }
	];

	// Server-side filters, sent as query parameters of /recommendations/active.
	let filterSource = '';
	let filterRisk = '';
	let filterCapability = '';
	let namespaceOptions = [];

	const SOURCE_OPTIONS = ['greenkube', 'vpa', 'karpenter'];
	const RISK_OPTIONS = [
		{ value: 'low', label: 'Low risk' },
		{ value: 'medium', label: 'Medium risk' },
		{ value: 'high', label: 'High risk' }
	];
	const CAPABILITY_OPTIONS = [
		{ value: 'cpu_rightsizing', label: 'CPU rightsizing' },
		{ value: 'memory_rightsizing', label: 'Memory rightsizing' },
		{ value: 'autoscaling', label: 'Autoscaling' },
		{ value: 'off_peak', label: 'Off-peak scaling' },
		{ value: 'carbon_aware', label: 'Carbon-aware scheduling' },
		{ value: 'zombie_cleanup', label: 'Zombie cleanup' },
		{ value: 'namespace_cleanup', label: 'Namespace cleanup' },
		{ value: 'node_optimization', label: 'Node optimization' },
		{ value: 'node_pool', label: 'Node pool' },
		{ value: 'storage_cleanup', label: 'Storage cleanup' },
		{ value: 'lb_cleanup', label: 'Load balancer cleanup' }
	];

	// Recommendation scan run (E1): requested explicitly, polled, then reloaded.
	let scanRun = null; // { run_id, status, recommendation_count?, error?, timedOut? }
	let scanError = '';
	let scanRequesting = false;
	let scanController = null;

	// Ignore modal state
	let ignoreModal = null; // { rec } | null
	let ignoreReason = '';
	let ignoreLoading = false;
	let ignoreError = null;

	// GitOps plan preview state
	let prModal = null; // { rec } | null
	let prPlan = null;
	let planApproval = null;
	let planApprovalBlocked = false;
	let planFeedback = '';
	let planPolling = false;
	let planPollingError = '';
	let prLoading = false;
	let prError = null;
	let chartMappingOptions = null;
	let chartMappingSelection = null;
	let chartMappingFile = '';
	let chartMappingContainer = '';
	let chartMappingPath = '';
	let modalTitleId = 'recommendation-modal-title';
	let planPollTimer = null;
	let planPollGeneration = 0;
	// Delivery modes (F4) and auto-approval (F5).
	let autoApproveBlockers = [];
	let deliveryPrUrl = '';
	let deliveryReport = null;
	let deliveryError = '';

	const PLAN_POLL_INTERVAL_MS = 3000;

	// Verification event trail per applied recommendation
	let eventsByRec = {}; // { [id]: Event[] }

	// Per-card action loading state
	let actionLoading = {}; // { [id]: bool }

	// Reload whenever the namespace or a server-side filter changes.
	$: rankingProfile = sortMode.startsWith('profile:') ? sortMode.slice('profile:'.length) : '';
	$: loadKey = [$selectedNamespace ?? '', filterSource, filterRisk, filterCapability, rankingProfile].join('|');
	$: if (loadKey !== undefined) loadData();

	let loadSeq = 0;
	let requestController = new AbortController();
	onDestroy(() => {
		requestController.abort();
		scanController?.abort();
		stopPlanStatusPolling();
		discardPreviewPlan();
	});

	onMount(() => {
		getNamespaces()
			.then((namespaces) => {
				namespaceOptions = Array.isArray(namespaces) ? namespaces : [];
			})
			.catch(() => {
				namespaceOptions = [];
			});
		followPendingScan();
	});

	async function loadData() {
		// Guard against overlapping loads: a slower response must not overwrite
		// the result of a newer request.
		const seq = ++loadSeq;
		requestController.abort();
		requestController = new AbortController();
		const { signal } = requestController;
		const namespace = $selectedNamespace || undefined;
		loading = true;
		error = null;
		automationStatus = null;
		automationStatusError = '';
		try {
			// Plain reads: page loads never schedule a scan (BUG-25).
			const [active, ignored, applied, sav, openPrs] = await Promise.all([
				getActiveRecommendations({
					namespace,
					source: filterSource || undefined,
					risk_level: filterRisk || undefined,
					capability: filterCapability || undefined,
					profile: rankingProfile || undefined,
					signal
				}),
				getIgnoredRecommendations({ namespace, signal }),
				getAppliedRecommendations({ namespace, signal }),
				getRecommendationSavings({ namespace, signal }),
				getOpenPullRequests({ signal })
			]);
			if (seq !== loadSeq) return;
			try {
				automationStatus = await getAutomationStatus({ signal });
			} catch (statusError) {
				if (statusError.name !== 'AbortError') {
					automationStatusError = statusError.message || 'Automation readiness could not be checked.';
				}
			}
			if (seq !== loadSeq) return;
			activeRecs = active;
			ignoredRecs = ignored;
			appliedRecs = applied;
			savings = sav;
			updateOpenPullRequests(openPrs, active);
		} catch (e) {
			if (seq === loadSeq && e.name !== 'AbortError') error = e.message;
		} finally {
			if (seq === loadSeq) loading = false;
		}
	}

	// --- Recommendation scan runs ---
	$: scanPending = scanRequesting || isRunPending(scanRun);

	async function followScan(run, signal) {
		scanRun = run;
		const finalRun = await waitForOptimizationRun(run, {
			signal,
			onUpdate: (update) => {
				scanRun = update;
			}
		});
		scanRun = finalRun;
		await loadData();
	}

	/** Page opened while a (scheduled) scan runs: reload when it completes. */
	async function followPendingScan() {
		scanController?.abort();
		const controller = new AbortController();
		scanController = controller;
		try {
			const run = await findPendingOptimizationRun({ signal: controller.signal });
			if (run && scanController === controller) await followScan(run, controller.signal);
		} catch (e) {
			if (e?.name !== 'AbortError' && scanController === controller) {
				scanError = e?.message || 'The recommendation scan status could not be read.';
			}
		}
	}

	/** Ask the controller for a fresh scan, then reload when it completes. */
	async function rescan() {
		if (scanPending) return;
		scanController?.abort();
		const controller = new AbortController();
		scanController = controller;
		scanError = '';
		scanRun = null;
		scanRequesting = true;
		try {
			const started = await startOptimizationScan({ signal: controller.signal });
			scanRequesting = false;
			if (!started.supported || !started.run) {
				// Older servers have no runs API: reload persisted results only.
				await loadData();
				return;
			}
			await followScan(started.run, controller.signal);
		} catch (e) {
			if (e?.name !== 'AbortError' && scanController === controller) {
				scanError = e?.message || 'The recommendation scan could not be requested.';
			}
		} finally {
			if (scanController === controller) scanRequesting = false;
		}
	}

	function scanStatusText(run) {
		const label = `Recommendation scan #${run.run_id}`;
		if (run.timedOut) return `${label} is still running. Results reload when you refresh again.`;
		if (run.status === 'queued') return `${label} is queued. Results reload automatically when it completes.`;
		if (run.status === 'running') return `${label} is running. Results reload automatically when it completes.`;
		if (run.status === 'succeeded') {
			const count = Number.isFinite(run.recommendation_count)
				? ` with ${run.recommendation_count} recommendation${run.recommendation_count === 1 ? '' : 's'}`
				: '';
			return `${label} completed${count}. Results reloaded.`;
		}
		if (run.status === 'failed') return `${label} failed${run.error ? `: ${run.error}` : '.'}`;
		return `${label} finished. Results reloaded.`;
	}

	function updateOpenPullRequests(openPrs, recommendations = activeRecs) {
		openPullRequests = openPrs;
		const openPrIds = new Set(openPrs.map((pr) => pr.recommendation_id));
		openPullRequestKeys = new Set(
			recommendations.filter((rec) => openPrIds.has(rec.id)).map((rec) => recommendationKey(rec))
		);
	}

	async function refreshOpenPullRequests() {
		try {
			updateOpenPullRequests(await getOpenPullRequests());
		} catch (e) {
			if (e.name !== 'AbortError') error = e.message;
		}
	}

	// --- Filters and ordering ---
	function enumValue(value) {
		return value === null || value === undefined ? '' : String(value);
	}

	/** Same exact-match semantics as the API filters, for the ignored list. */
	function matchesServerFilters(rec, source, risk, capability) {
		return (
			(!source || enumValue(rec.source) === source) &&
			(!risk || enumValue(rec.risk_level) === risk) &&
			(!capability || enumValue(rec.capability) === capability)
		);
	}

	$: serverFiltersActive = Boolean(filterSource || filterRisk || filterCapability);
	$: sourceOptions = [
		...new Set([
			...SOURCE_OPTIONS,
			...[...activeRecs, ...ignoredRecs].map((rec) => enumValue(rec.source)).filter(Boolean),
			...(filterSource ? [filterSource] : [])
		])
	];

	function clearFilters() {
		filterSource = '';
		filterRisk = '';
		filterCapability = '';
		filterType = 'all';
		selectedNamespace.set('');
	}

	$: openPrRecommendationIds = new Set(openPullRequests.map((pr) => pr.recommendation_id));
	$: actionableActiveRecs = activeRecs;
	$: currentList = activeTab === 'active'
		? actionableActiveRecs
		: ignoredRecs.filter((rec) => matchesServerFilters(rec, filterSource, filterRisk, filterCapability));
	$: types = [...new Set(currentList.map(r => r.type))];
	$: filtered = filterType === 'all'
		? currentList
		: currentList.filter(r => r.type === filterType);

	function savingsValue(rec, key) {
		const value = Number(rec?.[key]);
		return Number.isFinite(value) ? value : 0;
	}

	function sortRecommendations(list, mode) {
		if (mode === 'cost' || mode === 'co2') {
			const key = mode === 'cost' ? 'potential_savings_cost' : 'potential_savings_co2e_grams';
			// Stable: equal savings keep the server order.
			return [...list].sort((a, b) => savingsValue(b, key) - savingsValue(a, key));
		}
		// Server order: expected value under the selected profile.
		return list;
	}

	$: displayed = sortRecommendations(filtered, sortMode);

	function changeSort(mode) {
		sortMode = mode;
	}

	// IMP-07: the server's de-duplicated opportunity, the same figure as the dashboard. The client-side
	// sums (which add overlapping recommendations and net increases) are only a fallback.
	// The server figure covers every active recommendation: with source/risk filters the page sums the matches.
	$: opportunity = serverFiltersActive ? null : serverOpportunity(savings);
	$: totalSavingsCO2 =
		opportunity?.co2eGrams ?? actionableActiveRecs.reduce((s, r) => s + (r.potential_savings_co2e_grams ?? 0), 0);
	// An unknown billable saving (cluster not observed yet) is the allocated value: never summed as projected.
	$: totalSavingsCost =
		opportunity?.cost ??
		actionableActiveRecs.reduce(
			(s, r) => s + (r.projected_billable_cost_annual === null ? 0 : (r.potential_savings_cost ?? 0)),
			0
		);
	$: potentialSavingsPeriod = `per year · ${opportunity?.label ?? OPPORTUNITY_LABEL}`;
	$: savingsTabFigures = savingsFigures(savings);

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

	// --- GitOps plan preview ---
	function recommendationKey(rec) {
		return [
			rec.namespace ?? '',
			rec.type ?? '',
			rec.owner_kind ?? '',
			rec.owner_name ?? '',
			rec.container_name ?? ''
		].join('|');
	}

	function hasOpenPr(rec) {
		return openPrRecommendationIds.has(rec.id) || openPullRequestKeys.has(recommendationKey(rec));
	}

	/** The change set to plan, from the server's eligibility (F10); replaces client-side checks. */
	function safeActionFor(rec) {
		return planTargetFor(rec);
	}

	function hasSafePerContainerAction(rec) {
		return Boolean(safeActionFor(rec));
	}

	/** "Cost savings $120 · Incident risk -$8" in the recommendation's currency. */
	function describeFactorsInCurrency(rec) {
		const factors = rec?.ranking_factors;
		if (!factors || typeof factors !== 'object') return '';
		return Object.entries(factors)
			.filter(([, amount]) => typeof amount === 'number' && Number.isFinite(amount) && amount !== 0)
			.sort((a, b) => b[1] - a[1])
			.map(([key, amount]) => `${rankingFactorLabel(key)} ${formatMoney(amount, rec?.value?.currency)}`)
			.join(' · ');
	}

	function blockerText(entry) {
		return [entry.message, entry.fix].filter(Boolean).join(' ');
	}

	function manualReason(rec) {
		const eligibility = eligibilityOf(rec);
		if (eligibility?.canOpenPr) {
			return automationStatusError
				? 'Automation readiness could not be checked; pull-request preview is disabled.'
				: 'The automation worker is not ready; apply this recommendation manually.';
		}
		const [first] = eligibility?.blockers ?? [];
		return first ? blockerText(first) : 'This recommendation has no GitOps action planner; apply it manually.';
	}

	/** How approved plans become pull requests (F4): direct, patch or ci. */
	function deliveryMode() {
		const mode = automationStatus?.delivery_mode;
		return mode === 'patch' || mode === 'ci' ? mode : 'direct';
	}

	function approveLabel(mode = deliveryMode()) {
		if (mode === 'patch') return 'Approve and get the patch';
		if (mode === 'ci') return 'Approve for CI delivery';
		return 'Approve and open PR';
	}

	const AUTO_APPROVE_REASONS = {
		approve_scope_missing: 'your account may not approve plans',
		risk_above_auto_approve: 'the risk is above the auto-approval limit',
		confidence_below_auto_approve: 'the confidence is below the auto-approval limit',
		consensus_contested: 'sources disagree (contested advice is never approved automatically)',
		decrease_without_native_evidence: 'a decrease needs GreenKube evidence or two agreeing sources',
		increase_without_native_evidence: "an increase is only approved automatically when GreenKube's own analyzer sized it",
		increase_requires_approval: 'request increases need automation.autoApprove.increases',
		deletion_requires_review: 'a deletion is always approved by a person',
		auto_approve_failed: 'the automatic approval failed (approve it by hand)',
		not_active: 'a recommendation is not active',
		recommendation_missing: 'a recommendation could not be read',
		rollback_plan: 'rollbacks are always approved by a person',
		plan_not_validated: 'the preview is not validated'
	};

	/** Why automation.autoApprove left this preview for the click, or '' when it is off. */
	function autoApproveNote(blockers) {
		const reasons = (Array.isArray(blockers) ? blockers : [])
			.filter((code) => code !== 'auto_approve_disabled')
			.map((code) => AUTO_APPROVE_REASONS[code] ?? code);
		return reasons.length ? `Not approved automatically: ${reasons.join('; ')}.` : '';
	}

	/** "Collecting evidence — eligible in ~31h" while usage evidence warms up (F7). */
	function warmUpText(rec) {
		const eta = etaFrom(eligibilityOf(rec)?.eligibleAt ?? '');
		return eta ? `Collecting evidence — eligible in ${eta}` : '';
	}

	async function submitDeliveryLinkBack() {
		const preview = planPresentation(prPlan);
		const prUrl = deliveryPrUrl.trim();
		if (!preview.planId || !prUrl || prLoading) return;
		const modal = prModal;
		prLoading = true;
		deliveryError = '';
		try {
			const result = await reportPlanDelivery(preview.planId, prUrl);
			if (prModal !== modal) return;
			deliveryReport = result;
			if (result?.plan) prPlan = result.plan;
		} catch (e) {
			if (prModal === modal) deliveryError = e?.message ?? 'The pull request could not be linked.';
		} finally {
			if (prModal === modal) prLoading = false;
		}
	}

	function canPreviewRecommendation(rec) {
		return Boolean(
			automationStatus?.enabled === true &&
				automationStatus?.ready === true &&
				safeActionFor(rec) &&
				!hasOpenPr(rec)
		);
	}

	const PLAN_ERROR_MESSAGES = {
		unsupported_source: 'This GitOps source or renderer is not supported for plan preview.',
		needs_chart_mapping:
			'This Argo CD Helm source needs a one-time values mapping. Choose a Git-managed values file, target container, and resources key path below if mapping choices are available.',
		ambiguous_binding: 'The GitOps binding is ambiguous. Resolve the workload binding before creating a plan.',
		plan_stale: 'This plan is stale because its source changed. Preview a new plan before approving.',
		source_changed: 'The source changed after planning. Refresh and preview a new plan.',
		plan_expired: 'This plan has expired. Preview a new plan before approving.',
		revision_conflict: 'The source revision changed. Preview a new plan before approving.',
		provider_not_ready: 'The Git provider is unavailable or not configured for plan preview.',
		provider_capability_missing: 'The configured Git provider does not support the required plan operation.',
		requires_container_target: 'This recommendation does not have a safe per-container action target.',
		rollback_not_ready: 'This plan is not ready for rollback preview.',
		rollback_unsupported: 'This plan cannot be safely rolled back.',
		rollback_conflict: 'The original plan or current source changed; refresh before retrying.',
		plan_preview_unavailable: 'Plan preview is unavailable for this recommendation.',
		plan_preparation_unavailable: 'Server-side plan preparation is unavailable for this recommendation.',
		workload_plan_already_exists:
			'Another plan for this workload is still active. It is either an approved plan that is still opening its pull request, or an earlier preview that this GreenKube server cannot replace automatically. Unapproved previews expire after 30 minutes: preview again then, or check the open pull requests.'
	};

	function planErrorCode(value) {
		const statusCodes = {
			unsupported: 'unsupported_source',
			ambiguous: 'ambiguous_binding',
			stale: 'plan_stale',
			needs_mapping: 'needs_chart_mapping',
			plan_preview_unavailable: 'plan_preview_unavailable',
			plan_preparation_unavailable: 'plan_preparation_unavailable',
			unsupported_source: 'unsupported_source',
			ambiguous_binding: 'ambiguous_binding',
			plan_stale: 'plan_stale',
			provider_not_ready: 'provider_not_ready',
			provider_capability_missing: 'provider_capability_missing',
			rollback_not_ready: 'rollback_not_ready',
			rollback_unsupported: 'rollback_unsupported',
			rollback_conflict: 'rollback_conflict'
		};
		const statusCode = [
			value?.binding_status,
			value?.source?.status,
			value?.binding?.status,
			value?.status
		]
			.map((status) => (typeof status === 'string' ? statusCodes[status.toLowerCase()] : ''))
			.find(Boolean);
		const code =
			value?.code ??
			value?.error_code ??
			value?.error?.code ??
			value?.detail?.code ??
			value?.detail?.error_code ??
			statusCode ??
			'';
		if (code) return String(code).toLowerCase();
		const text = [
			value?.message,
			typeof value?.detail === 'string' ? value.detail : value?.detail?.message,
			value?.error?.message
		]
			.filter(Boolean)
			.join(' ');
		if (/plan[\s_-]+preview[\s_-]+unavailable/i.test(text)) return 'plan_preview_unavailable';
		const match = text.match(
			/\b(plan_preview_unavailable|plan_preparation_unavailable|unsupported_source|needs_chart_mapping|ambiguous_binding|plan_stale|source_changed|plan_expired|revision_conflict|provider_not_ready|provider_capability_missing|requires_container_target|rollback_not_ready|rollback_unsupported|rollback_conflict|workload_plan_already_exists)\b/i
		);
		return match?.[1]?.toLowerCase() ?? '';
	}

	function planErrorMessage(value) {
		const code = planErrorCode(value);
		if (PLAN_ERROR_MESSAGES[code]) return PLAN_ERROR_MESSAGES[code];
		return (
			value?.message ??
			(typeof value?.detail === 'string' ? value.detail : value?.detail?.message) ??
			value?.error?.message ??
			'Could not prepare an automation plan.'
		);
	}

	function planNeedsReplan(value) {
		return ['plan_stale', 'source_changed', 'plan_expired', 'revision_conflict'].includes(
			planErrorCode(value)
		);
	}

	function comparableRevision(value) {
		if (typeof value !== 'string') return '';
		const revision = value.trim().toLowerCase();
		const embedded = revision.match(/@sha(?:1|256):([0-9a-f]{40}|[0-9a-f]{64})$/);
		return embedded?.[1] ?? revision;
	}

	function planPresentation(response) {
		const plan = response?.plan ?? response ?? {};
		const filePlan = plan.file_plan ?? plan.filePlan ?? {};
		const source = plan.source ?? plan.binding?.binding ?? {};
		const validation = plan.validation ?? filePlan.validation ?? plan.render_validation ?? {};
		const rawPolicyResult =
			plan.policy_result ?? plan.policyResult ?? response?.policy_result ?? response?.policyResult;
		const policyResult =
			rawPolicyResult && typeof rawPolicyResult === 'object' && !Array.isArray(rawPolicyResult)
				? rawPolicyResult
				: null;
		const rawFiles = plan.files ?? filePlan.files ?? [];
		const files = Array.isArray(rawFiles) ? rawFiles : [];
		const rawWarnings = [
			...(Array.isArray(plan.warnings) ? plan.warnings : []),
			...(Array.isArray(filePlan.warnings) ? filePlan.warnings : []),
			...(Array.isArray(validation.warnings) ? validation.warnings : [])
		];
		const warnings = [...new Set(rawWarnings.map((warning) => {
			if (warning === 'render-warning') {
				return 'Renderer reported a non-blocking warning; details are omitted for safety.';
			}
			if (typeof warning === 'string') return warning;
			return warning?.message ?? warning?.detail ?? '';
		}).filter(Boolean))];
		const controllerValue = source.controller;
		const controller =
			(typeof controllerValue === 'string' && controllerValue) ||
			(controllerValue && typeof controllerValue === 'object'
				? [
						controllerValue.kind ?? controllerValue.controller_kind,
						controllerValue.name ?? controllerValue.controller_name
					]
						.filter(Boolean)
						.join('/')
				: '') ||
			[source.controller_kind, source.controller_name]
				.filter(Boolean)
				.join('/');
		const validationPassed =
			typeof validation.passed === 'boolean'
				? validation.passed
				: typeof validation.target_changed === 'boolean' &&
					  typeof validation.unrelated_changes === 'boolean' &&
					  typeof validation.requests_within_limits === 'boolean'
					? validation.target_changed &&
					  !validation.unrelated_changes &&
					  validation.requests_within_limits
					: false;
		const expiresAt = plan.expires_at ?? plan.expiresAt ?? '';
		const expiryTimestamp = Date.parse(expiresAt);
		const baseRevision =
			filePlan.base_revision ?? source.base_revision ?? source.revision ?? '';
		const controllerRevision = source.source_revision ?? '';
		const revisionDrift =
			Boolean(comparableRevision(baseRevision)) &&
			Boolean(comparableRevision(controllerRevision)) &&
			comparableRevision(baseRevision) !== comparableRevision(controllerRevision);
		return {
			plan,
			filePlan,
			source,
			validation,
			policyResult,
			files,
			warnings,
			controller,
			repository:
				source.repo_url ?? source.repo ?? source.repository ?? source.repository_url ?? '',
			path: source.path ?? '',
			renderer: source.renderer ?? filePlan.renderer ?? validation.renderer ?? plan.renderer ?? '',
			sourceRevision: baseRevision || controllerRevision || filePlan.source_revision || '',
			baseRevision,
			controllerRevision,
			revisionDrift,
			sourceDiff: plan.source_diff ?? plan.diff ?? filePlan.source_diff ?? filePlan.diff ?? '',
			renderedDiff: plan.rendered_diff ?? filePlan.rendered_diff ?? '',
			digest: plan.digest ?? filePlan.digest ?? '',
			planId: plan.plan_id ?? plan.id ?? '',
			operationId: plan.operation_id ?? plan.operationId ?? '',
			status: String(plan.status ?? '').toLowerCase(),
			operationStatus: String(plan.operation_status ?? '').toLowerCase(),
			rollbackOfPlanId: plan.rollback_of_plan_id ?? '',
			expiresAt,
			expiryValid: Number.isFinite(expiryTimestamp) && expiryTimestamp > Date.now(),
			bindingStatus: String(source.status ?? source.binding_status ?? plan.binding_status ?? '').toLowerCase(),
			validationPassed
		};
	}

	function canApprovePlan(response) {
		const preview = planPresentation(response);
		return Boolean(
			preview.planId &&
				preview.status === 'validated' &&
				preview.digest &&
			preview.expiryValid &&
				preview.controller &&
				preview.repository &&
				preview.path &&
				preview.renderer &&
				preview.files.length > 0 &&
				preview.sourceDiff &&
				preview.renderedDiff &&
				preview.validationPassed &&
				(!preview.bindingStatus || preview.bindingStatus === 'resolved')
		);
	}

	function canCancelPlan(response) {
		const preview = planPresentation(response);
		return Boolean(
			preview.planId &&
				['draft', 'validated', 'approved'].includes(preview.status) &&
				!planApprovalBlocked
		);
	}

	function canReplan(response) {
		const preview = planPresentation(response);
		return Boolean(
			prModal &&
				safeActionFor(prModal.rec) &&
				!preview.rollbackOfPlanId &&
				(
					planApprovalBlocked ||
					['stale', 'failed', 'closed', 'superseded', 'rejected', 'cancelled'].includes(preview.status) ||
					(preview.status === 'validated' && preview.expiresAt && !preview.expiryValid)
				)
		);
	}

	function canPreviewRollback(response) {
		const preview = planPresentation(response);
		return Boolean(
			preview.planId &&
				['synced', 'applied'].includes(preview.status) &&
				!preview.rollbackOfPlanId
		);
	}

	function operationStatusLabel(value) {
		const labels = {
			queued: 'Queued',
			running: 'Running',
			retrying: 'Retrying',
			succeeded: 'Succeeded',
			failed: 'Failed',
			cancelled: 'Cancelled'
		};
		return labels[String(value ?? '').toLowerCase()] ?? 'Unknown';
	}

	function isOperationPending(response) {
		return ['queued', 'running', 'retrying'].includes(planPresentation(response).operationStatus);
	}

	function stopPlanStatusPolling() {
		planPollGeneration += 1;
		if (planPollTimer !== null) clearTimeout(planPollTimer);
		planPollTimer = null;
		planPolling = false;
	}

	function schedulePlanStatusPolling(modal = prModal) {
		if (!modal || modal !== prModal || !planApproval || !isOperationPending(prPlan)) {
			if (!planApproval || !isOperationPending(prPlan)) {
				if (planPollTimer !== null) clearTimeout(planPollTimer);
				planPollTimer = null;
				planPolling = false;
			}
			return;
		}

		if (planPollTimer !== null) clearTimeout(planPollTimer);
		const generation = planPollGeneration;
		const preview = planPresentation(prPlan);
		if (!preview.planId) return;

		planPollTimer = setTimeout(async () => {
			if (generation !== planPollGeneration || prModal !== modal) return;
			planPollTimer = null;
			planPolling = true;
			try {
				// The plan carries its operation state (operation_id, operation_status), so one
				// endpoint is polled; the legacy /automation/operations route was removed (F5).
				const response = await getAutomationPlan(preview.planId);
				if (generation !== planPollGeneration || prModal !== modal) return;
				prPlan = response?.plan ?? response;
				planPollingError = '';
				if (planPresentation(prPlan).status === 'pr_open') {
					await refreshOpenPullRequests();
				}
			} catch {
				if (generation === planPollGeneration && prModal === modal) {
					planPollingError = 'Operation status could not be refreshed; automatic polling will retry.';
				}
			} finally {
				if (generation === planPollGeneration && prModal === modal) {
					planPolling = false;
					schedulePlanStatusPolling(modal);
				}
			}
		}, PLAN_POLL_INTERVAL_MS);
	}

	function planResponseError(response) {
		return planErrorCode(response) ? planErrorMessage(response) : null;
	}

	function applyPlanPreview(response, feedback = '') {
		const responseError = planResponseError(response);
		if (responseError) {
			if (response?.policy_result || response?.policyResult) {
				prPlan = { status: 'rejected', policy_result: response.policy_result ?? response.policyResult };
			}
			prError = responseError;
			planApprovalBlocked = planNeedsReplan(response);
			return false;
		}
		prPlan = response?.plan ?? response;
		autoApproveBlockers = Array.isArray(response?.auto_approve_blockers) ? response.auto_approve_blockers : [];
		if (response?.auto_approved === true) {
			// automation.autoApprove approved the digest with the preview (F5): no extra click.
			planApproval = response;
			planFeedback = 'Approved automatically by the automation.autoApprove policy.';
			if (prModal) schedulePlanStatusPolling(prModal);
			return true;
		}
		if (!canApprovePlan(prPlan)) {
			const preview = planPresentation(prPlan);
			prError = preview.expiresAt && !preview.expiryValid
				? PLAN_ERROR_MESSAGES.plan_expired
				: 'Plan approval is unavailable until the API returns a validated preview with a resolved source, files, diffs, digest, and expiry.';
			return false;
		}
		planFeedback = feedback;
		return true;
	}

	async function requestPlanPreview(rec, modal, feedback = '', valuesMapping = null) {
		const action = safeActionFor(rec);
		if (!action || prModal !== modal) return;
		stopPlanStatusPolling();
		prPlan = null;
		planApproval = null;
		planApprovalBlocked = false;
		planFeedback = '';
		planPollingError = '';
		prError = null;
		prLoading = true;
		try {
			const response = valuesMapping
				? await createAutomationPlan(action.recommendationIds, valuesMapping)
				: await createAutomationPlan(action.recommendationIds);
			if (prModal !== modal) {
				// The dialog closed while the preview was prepared: do not leave it active.
				discardPreviewPlan(response?.plan ?? response, null);
				return;
			}
			applyPlanPreview(response, feedback);
		} catch (e) {
			if (prModal === modal) {
				prError = planErrorMessage(e);
				planApprovalBlocked = planNeedsReplan(e);
				const options = e?.mappingOptions ?? e?.mapping_options;
				if (planErrorCode(e) === 'needs_chart_mapping' && options) {
					chartMappingOptions = options;
					const files = Array.isArray(options.files) ? options.files : [];
					const containers = Array.isArray(options.containers) ? options.containers : [];
					if (!files.includes(chartMappingFile)) chartMappingFile = files[0] ?? '';
					if (!containers.includes(chartMappingContainer)) {
						chartMappingContainer = containers[0] ?? '';
					}
					if (!chartMappingPath && chartMappingSelection?.path) {
						chartMappingPath = chartMappingSelection.path.join('.');
					}
				}
				if (e?.policyResult) prPlan = { status: 'rejected', policy_result: e.policyResult };
			}
		} finally {
			if (prModal === modal) prLoading = false;
		}
	}

	async function submitChartMapping() {
		const modal = prModal;
		if (!modal || prLoading || !chartMappingOptions) return;
		const path = chartMappingPath.split('.').map((segment) => segment.trim());
		if (
			!chartMappingOptions.files?.includes(chartMappingFile) ||
			!chartMappingOptions.containers?.includes(chartMappingContainer) ||
			!path.length ||
			path.some((segment) => !segment) ||
			path.length > 32 ||
			path[path.length - 1] !== 'resources'
		) {
			prError = 'Select an available values file and container, and enter a key path ending in resources.';
			return;
		}
		const valuesMapping = {
			file_path: chartMappingFile,
			container: chartMappingContainer,
			path
		};
		chartMappingSelection = valuesMapping;
		await requestPlanPreview(
			modal.rec,
			modal,
			'One-time mapping validated. Review the plan before approving.',
			valuesMapping
		);
	}

	async function openPrModal(rec) {
		const action = safeActionFor(rec);
		if (!action || hasOpenPr(rec)) return;
		const modal = { rec };
		prModal = modal;
		chartMappingOptions = null;
		chartMappingSelection = null;
		chartMappingFile = '';
		chartMappingContainer = '';
		chartMappingPath = '';
		await requestPlanPreview(rec, modal);
	}

	async function rePlan() {
		const modal = prModal;
		if (!modal || !canReplan(prPlan)) return;
		await requestPlanPreview(
			modal.rec,
			modal,
			'New plan preview ready. Review it before approving.',
			chartMappingSelection
		);
	}

	const DISCARDABLE_PLAN_STATUSES = ['draft', 'validated'];

	// Plans whose approval was requested from this page. They are never
	// discarded: the approval may still be in flight (the dialog can close or
	// the page unmount meanwhile), and its outcome is ambiguous after a network
	// or gateway error. An unapproved leftover expires or is superseded (D6).
	const approvalRequestedPlanIds = new Set();

	/**
	 * Discard an unapproved preview when its dialog closes, so previewing the
	 * same workload again is not blocked by WORKLOAD_PLAN_ALREADY_EXISTS (D6).
	 * Best effort: a preview that cannot be discarded still expires on its own.
	 */
	function discardPreviewPlan(plan = prPlan, approval = planApproval) {
		if (!plan || approval) return;
		const preview = planPresentation(plan);
		if (!preview.planId || approvalRequestedPlanIds.has(preview.planId)) return;
		if (!DISCARDABLE_PLAN_STATUSES.includes(preview.status)) return;
		Promise.resolve()
			.then(() => discardAutomationPlan(preview.planId))
			.catch(() => {});
	}

	function closePrModal() {
		stopPlanStatusPolling();
		discardPreviewPlan();
		prModal = null;
		prPlan = null;
		planApproval = null;
		planApprovalBlocked = false;
		planFeedback = '';
		planPollingError = '';
		prError = null;
		prLoading = false;
		autoApproveBlockers = [];
		deliveryPrUrl = '';
		deliveryReport = null;
		deliveryError = '';
		chartMappingOptions = null;
		chartMappingSelection = null;
		chartMappingFile = '';
		chartMappingContainer = '';
		chartMappingPath = '';
	}

	async function approvePlan() {
		if (!prModal || !canApprovePlan(prPlan)) return;
		const modal = prModal;
		const preview = planPresentation(prPlan);
		stopPlanStatusPolling();
		prLoading = true;
		prError = null;
		planFeedback = '';
		planPollingError = '';
		planApproval = null;
		if (preview.planId) approvalRequestedPlanIds.add(preview.planId);
		try {
			const result = await approveAutomationPlan(preview.planId, preview.digest);
			if (prModal !== modal) return;
			planApproval = result;
			if (result?.plan_id || result?.plan) {
				prPlan = result.plan ?? result;
			}
			if (['error', 'failed', 'cancelled', 'stale'].includes(String(result?.status).toLowerCase())) {
				prError = planErrorMessage(result);
				planApprovalBlocked = planNeedsReplan(result);
			}
		} catch (e) {
			if (prModal === modal) {
				prError = planErrorMessage(e);
				planApprovalBlocked = planNeedsReplan(e);
				if (e?.policyResult) {
					prPlan = { ...(prPlan ?? {}), policy_result: e.policyResult };
				}
			}
		} finally {
			if (prModal === modal) {
				prLoading = false;
				schedulePlanStatusPolling(modal);
			}
		}
	}

	async function cancelPlan() {
		if (!prModal || !canCancelPlan(prPlan) || prLoading) return;
		const modal = prModal;
		const preview = planPresentation(prPlan);
		stopPlanStatusPolling();
		prLoading = true;
		prError = null;
		planFeedback = '';
		planPollingError = '';
		try {
			const result = await cancelAutomationPlan(preview.planId);
			if (prModal !== modal) return;
			prPlan = result?.plan ?? result;
			planApproval = null;
			// "cancelled" since migration 0220; older servers return "stale".
			planApprovalBlocked = ['stale', 'cancelled'].includes(String(prPlan?.status ?? '').toLowerCase());
			planFeedback = planApprovalBlocked
				? 'Plan cancelled. Preview a new plan before approving.'
				: 'Plan status updated.';
		} catch (e) {
			if (prModal === modal) prError = planErrorMessage(e);
		} finally {
			if (prModal === modal) prLoading = false;
		}
	}

	async function previewPlanRollback() {
		if (!prModal || !canPreviewRollback(prPlan) || prLoading) return;
		const modal = prModal;
		const preview = planPresentation(prPlan);
		stopPlanStatusPolling();
		prLoading = true;
		prError = null;
		planFeedback = '';
		planPollingError = '';
		planApproval = null;
		planApprovalBlocked = false;
		try {
			const result = await previewAutomationPlanRollback(preview.planId);
			if (prModal !== modal) {
				discardPreviewPlan(result?.plan ?? result, null);
				return;
			}
			applyPlanPreview(result, 'Rollback preview ready. Review it before approving.');
		} catch (e) {
			if (prModal === modal) prError = planErrorMessage(e);
		} finally {
			if (prModal === modal) prLoading = false;
		}
	}

	async function refreshPlanStatus() {
		if (!prPlan || prLoading) return;
		const modal = prModal;
		const preview = planPresentation(prPlan);
		if (!preview.planId) return;
		stopPlanStatusPolling();
		prLoading = true;
		prError = null;
		planPollingError = '';
		try {
			const result = await getAutomationPlan(preview.planId);
			if (prModal !== modal) return;
			prPlan = result?.plan ?? result;
			if (String(prPlan?.status ?? '').toLowerCase() === 'pr_open') {
				await refreshOpenPullRequests();
			}
		} catch (e) {
			if (prModal === modal) prError = planErrorMessage(e);
		} finally {
			if (prModal === modal) {
				prLoading = false;
				schedulePlanStatusPolling(modal);
			}
		}
	}

	function planLifecycleLabel(status) {
		const labels = {
			draft: 'Draft plan',
			validated: 'Validated; awaiting approval',
			approved: 'Queued for a worker',
			applying: 'Creating the pull request',
			pr_open: 'Pull request open',
			merged: 'Merged; waiting for controller sync',
			synced: 'Synced; waiting for workload apply',
			sync_pending: 'Waiting for workload apply',
			sync_failed: 'Sync failed; operator attention required',
			applied: 'Applied to the workload',
			closed: 'Pull request closed without merge',
			failed: 'Plan execution failed',
			stale: 'Plan is stale',
			needs_mapping: 'Chart mapping required',
			ambiguous: 'Workload binding is ambiguous',
			rejected: 'Plan rejected',
			superseded: 'Plan superseded',
			cancelled: 'Plan cancelled'
		};
		const normalized = String(status ?? '').toLowerCase();
		return labels[normalized] ?? (normalized || 'Unknown');
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

	/**
	 * Request change of an applied recommendation, per resource: the baseline
	 * before the change, the proposal and the live value detected after it.
	 * `unchanged` flags a recorded change whose live value still equals the
	 * baseline, so it is never shown as a pointless "400m → 400m".
	 */
	function requestChanges(rec) {
		const changes = [];
		for (const [resource, label, format, before, proposed, after] of [
			['cpu', 'CPU', formatCPU, rec.current_cpu_request_millicores, rec.recommended_cpu_request_millicores, rec.actual_cpu_request_millicores],
			['memory', 'Memory', formatBytes, rec.current_memory_request_bytes, rec.recommended_memory_request_bytes, rec.actual_memory_request_bytes]
		]) {
			if (before == null) continue;
			changes.push({
				resource,
				label,
				format,
				before,
				proposed: proposed ?? null,
				after: after ?? null,
				unchanged: after != null && after === before
			});
		}
		return changes;
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

	const DIALOG_FOCUSABLE_SELECTOR =
		'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

	function trapDialogFocus(node) {
		const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		const focusableElements = () =>
			Array.from(node.querySelectorAll(DIALOG_FOCUSABLE_SELECTOR)).filter(
				(element) =>
					!element.hasAttribute('hidden') &&
					element.getAttribute('aria-hidden') !== 'true' &&
					!element.closest('[hidden], [inert]')
			);

		function handleTab(event) {
			if (event.key !== 'Tab') return;
			const elements = focusableElements();
			if (elements.length === 0) {
				event.preventDefault();
				node.focus();
				return;
			}
			const first = elements[0];
			const last = elements[elements.length - 1];
			if (event.shiftKey && (document.activeElement === first || !node.contains(document.activeElement))) {
				event.preventDefault();
				last.focus();
			} else if (!event.shiftKey && (document.activeElement === last || !node.contains(document.activeElement))) {
				event.preventDefault();
				first.focus();
			}
		}

		node.addEventListener('keydown', handleTab);
		(focusableElements()[0] ?? node).focus();
		return {
			destroy() {
				node.removeEventListener('keydown', handleTab);
				if (previousFocus?.isConnected) previousFocus.focus();
			}
		};
	}

	function handleEscape(event) {
		if (event.key === 'Escape') {
			if (ignoreModal) closeIgnoreModal();
			else if (prModal) closePrModal();
		}
	}

</script>

<svelte:window on:keydown={handleEscape} />

<!-- ─── Ignore Modal ─────────────────────────────────────────────────────── -->
{#if ignoreModal}
	<!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
	<div
		role="presentation"
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4"
		on:click|self={closeIgnoreModal}
	>
		<div
			class="bg-dark-900 border border-dark-700 rounded-xl shadow-2xl w-full max-w-md p-6 space-y-4"
			role="dialog"
			aria-modal="true"
			aria-labelledby={modalTitleId}
			tabindex="-1"
			use:trapDialogFocus
		>
			<div class="flex items-start justify-between gap-4">
				<div>
					<h2 id={modalTitleId} class="text-base font-semibold text-dark-100">Ignore recommendation</h2>
					<p class="text-xs text-dark-500 mt-1 break-words">
						{getTypeConfig(ignoreModal.rec.type).icon}
						{ignoreModal.rec.pod_name ?? ignoreModal.rec.target_node ?? ignoreModal.rec.namespace ?? 'Cluster-wide'}
						— {getTypeConfig(ignoreModal.rec.type).label}
					</p>
				</div>
				<button
					aria-label="Close ignore recommendation dialog"
					class="text-dark-500 hover:text-dark-200 transition-colors text-xl leading-none shrink-0"
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
					       placeholder-dark-600 focus:outline-hidden focus:border-green-600 resize-none"
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

<!-- ─── Plan Preview / Pull Request Modal ─────────────────────────────────── -->
{#if prModal}
	<!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
	<div
		role="presentation"
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs p-4"
		on:click|self={closePrModal}
	>
		<div
			class="bg-dark-900 border border-dark-700 rounded-xl shadow-2xl w-full max-w-2xl p-6 space-y-4 max-h-[85vh] overflow-y-auto"
			role="dialog"
			aria-modal="true"
			aria-labelledby={modalTitleId}
			aria-busy={prLoading}
			tabindex="-1"
			use:trapDialogFocus
		>
			<div class="flex items-start justify-between gap-4">
				<div>
					<h2 id={modalTitleId} class="text-base font-semibold text-dark-100">Review automation plan</h2>
					<p class="text-xs text-dark-500 mt-1 break-words">
						{getTypeConfig(prModal.rec.type).icon}
						{prModal.rec.owner_kind ? `${prModal.rec.owner_kind}/${prModal.rec.owner_name}` : prModal.rec.pod_name}
						— {getTypeConfig(prModal.rec.type).label}
					</p>
				</div>
				<button
					aria-label="Close pull request dialog"
					class="text-dark-500 hover:text-dark-200 transition-colors text-xl leading-none shrink-0"
					on:click={closePrModal}
				>✕</button>
			</div>

			<OomHistoryWarning warning={oomHistoryWarningOf(prModal.rec)} />

			{#if prLoading && !prPlan}
				<p class="text-xs text-dark-400 flex items-center gap-2"><span class="animate-spin">⟳</span> Preparing preview…</p>
			{/if}

			{#if prError}
				<div
					role="alert"
					class="text-xs text-red-400 bg-red-600/10 border border-red-600/30 rounded-lg px-3 py-2"
				>
					{prError}
				</div>
			{/if}

			{#if chartMappingOptions && !prPlan}
				<form
					class="space-y-3 rounded-lg border border-dark-700 bg-dark-950/60 p-3"
					on:submit|preventDefault={submitChartMapping}
				>
					<div>
						<h3 class="text-xs font-semibold uppercase tracking-wide text-dark-300">One-time Helm values mapping</h3>
						<p class="mt-1 text-xs text-dark-500">
							This choice is validated against the server-resolved Git snapshot and applies only to this plan.
							It does not edit the Argo CD Application or send repository credentials.
						</p>
					</div>
					<label class="block space-y-1 text-xs text-dark-400">
						<span>Git-managed values file</span>
						<select
							aria-label="Git-managed values file"
							class="w-full rounded-sm border border-dark-700 bg-dark-900 px-2 py-1.5 text-dark-100"
							bind:value={chartMappingFile}
							required
							disabled={prLoading}
						>
							{#each chartMappingOptions.files ?? [] as filePath}
								<option value={filePath}>{filePath}</option>
							{/each}
						</select>
					</label>
					<label class="block space-y-1 text-xs text-dark-400">
						<span>Container</span>
						<select
							aria-label="Container"
							class="w-full rounded-sm border border-dark-700 bg-dark-900 px-2 py-1.5 text-dark-100"
							bind:value={chartMappingContainer}
							required
							disabled={prLoading}
						>
							{#each chartMappingOptions.containers ?? [] as container}
								<option value={container}>{container}</option>
							{/each}
						</select>
					</label>
					<label class="block space-y-1 text-xs text-dark-400">
						<span>Resources key path</span>
						<input
							aria-label="Resources key path"
							class="w-full rounded-sm border border-dark-700 bg-dark-900 px-2 py-1.5 text-dark-100"
							type="text"
							placeholder="app.resources"
							bind:value={chartMappingPath}
							required
							disabled={prLoading}
						/>
						<span class="block text-[11px] text-dark-600">Use dot-separated keys ending in resources.</span>
					</label>
					<button
						class="btn-primary text-xs disabled:opacity-50"
						type="submit"
						disabled={prLoading || !chartMappingFile || !chartMappingContainer || !chartMappingPath.trim()}
					>
						{prLoading ? 'Validating mapping…' : 'Validate mapping and preview'}
					</button>
				</form>
			{/if}

			{#if planFeedback && !prError}
				<p class="text-xs text-green-400" role="status">{planFeedback}</p>
			{/if}

			{#if prPlan && !planApproval && autoApproveNote(autoApproveBlockers)}
				<p class="text-xs text-dark-400" data-testid="auto-approve-note">{autoApproveNote(autoApproveBlockers)}</p>
			{/if}

			{#if planApproval && !prError && (prPlan?.delivery_mode === 'patch' || prPlan?.delivery_mode === 'ci')}
				<div class="text-xs rounded-lg px-3 py-2 border border-blue-600/30 bg-blue-600/10 text-blue-200 space-y-2" data-testid="external-delivery">
					{#if prPlan.delivery_mode === 'ci'}
						<p>Approved for CI delivery: your CI job opens the pull request with its own token and reports it back. GreenKube then tracks it to merge and sync.</p>
					{:else}
						<p>
							Approved. <a class="underline underline-offset-2" href={planPatchUrl(planPresentation(prPlan).planId)} download>Download the patch</a>
							or run <code>greenkube plan apply-patch {planPresentation(prPlan).planId}</code>, push the branch and open the pull request.
							GreenKube links it by its branch name or its GreenKube-Plan trailer.
						</p>
					{/if}
					{#if deliveryReport?.pull_request}
						<p role="status">Pull request #{deliveryReport.pull_request.number} linked; GreenKube tracks it now.</p>
					{:else if prPlan.status === 'approved'}
						<form class="flex flex-wrap items-end gap-2" on:submit|preventDefault={submitDeliveryLinkBack}>
							<label class="flex-1 min-w-[16rem]">
								<span class="block text-[11px] text-dark-400">I opened PR</span>
								<input
									class="input text-xs w-full"
									type="url"
									placeholder="https://github.com/org/repo/pull/42"
									bind:value={deliveryPrUrl}
									disabled={prLoading}
								/>
							</label>
							<button class="btn-secondary text-xs disabled:opacity-50" type="submit" disabled={prLoading || !deliveryPrUrl.trim()}>
								Link pull request
							</button>
						</form>
						{#if deliveryError}<p class="text-red-400" role="alert">{deliveryError}</p>{/if}
					{/if}
				</div>
			{/if}

			{#if planApproval && !prError}
				<div
					role={prPlan?.status === 'sync_failed' || prPlan?.operation_status === 'failed' ? 'alert' : 'status'}
					class={`text-xs rounded-lg px-3 py-2 border ${
						prPlan?.status === 'sync_failed' || prPlan?.operation_status === 'failed'
							? 'text-red-400 bg-red-600/10 border-red-600/30'
							: 'text-green-400 bg-green-600/10 border-green-600/30'
					}`}
				>
					Plan approved: {planApproval.status ?? 'approved'}
					{#if planApproval.operation_id} · Operation {planApproval.operation_id}{/if}
					<span class="block mt-1">Plan lifecycle: {planLifecycleLabel(prPlan?.status ?? planApproval.status)}</span>
					{#if prPlan?.operation_status}
						<span class="block mt-1">
							Operation status: {operationStatusLabel(prPlan.operation_status)}
							{#if isOperationPending(prPlan)}
								· {planPolling ? 'Refreshing now…' : 'Polling automatically'}
							{/if}
						</span>
					{/if}
					{#if planPollingError}
						<span class="block mt-1 text-yellow-300">{planPollingError}</span>
					{/if}
					<button
						type="button"
						class="mt-2 underline underline-offset-2 disabled:opacity-50"
						disabled={prLoading}
						on:click={refreshPlanStatus}
					>
						{prLoading ? 'Refreshing…' : 'Refresh plan status'}
					</button>
				</div>
			{/if}

			{#if prPlan}
				{@const preview = planPresentation(prPlan)}
				{@const policyChecks = Array.isArray(preview.policyResult?.checks) ? preview.policyResult.checks : []}
				{@const policyRejections = Array.isArray(preview.policyResult?.rejections) ? preview.policyResult.rejections : []}
				<div class="space-y-4">
					<section class="space-y-2">
						<h3 class="text-xs font-semibold uppercase tracking-wide text-dark-300">Plan source</h3>
						<dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-2 text-xs">
							<div>
								<dt class="text-dark-500">Controller</dt>
								<dd class="text-dark-200 break-words">{preview.controller || 'Not provided'}</dd>
							</div>
							<div>
								<dt class="text-dark-500">Repository</dt>
								<dd class="text-dark-200 break-all">{preview.repository || 'Not provided'}</dd>
							</div>
							<div>
								<dt class="text-dark-500">Path</dt>
								<dd class="text-dark-200 break-all">{preview.path || 'Not provided'}</dd>
							</div>
							<div>
								<dt class="text-dark-500">Renderer</dt>
								<dd class="text-dark-200">{preview.renderer || 'Not provided'}</dd>
							</div>
							{#if preview.rollbackOfPlanId}
								<div class="sm:col-span-2">
									<dt class="text-dark-500">Rollback of</dt>
									<dd class="text-dark-200 break-all">{preview.rollbackOfPlanId}</dd>
								</div>
							{/if}
							{#if preview.sourceRevision}
								<div class="sm:col-span-2">
									<dt class="text-dark-500">Branch revision used by plan</dt>
									<dd class="text-dark-200 break-all">{preview.sourceRevision}</dd>
								</div>
							{/if}
							{#if preview.controllerRevision}
								<div class="sm:col-span-2">
									<dt class="text-dark-500">Controller-reported revision</dt>
									<dd class="text-dark-200 break-all">{preview.controllerRevision}</dd>
								</div>
							{/if}
						</dl>
						{#if preview.revisionDrift}
							<p role="alert" class="rounded-sm border border-amber-700/70 bg-amber-950/40 p-2 text-xs text-amber-200">
								The plan targets the current repository branch head, which differs from the revision reported by the GitOps controller. Refresh discovery or sync the controller before approving if the live revision is required.
							</p>
						{/if}
					</section>

					{#if preview.policyResult}
						<section class="space-y-2 rounded-lg border border-dark-700 bg-dark-950/60 p-3">
							<h3 class="text-xs font-semibold uppercase tracking-wide text-dark-300">Automation policy</h3>
							<p class="text-xs {preview.policyResult.allowed ? 'text-green-400' : 'text-amber-400'}">
								{preview.policyResult.allowed ? 'Policy checks passed' : 'Policy blocks this plan'}
							</p>
							{#if policyChecks.length}
								<ul class="space-y-2">
									{#each policyChecks as check}
										<li class="text-xs">
											<div class="flex flex-wrap items-center gap-2">
												<span class="text-dark-200">{String(check.gate ?? 'policy').replaceAll('_', ' ')}</span>
												<span class="rounded-sm bg-dark-800 px-1.5 py-0.5 text-[10px] text-dark-400">
													{String(check.state ?? 'unknown').replaceAll('_', ' ')}
												</span>
											</div>
											<p class="mt-1 text-dark-400">{check.message ?? 'No policy explanation was returned.'}</p>
										</li>
									{/each}
								</ul>
							{/if}
							{#if policyRejections.length}
								<p class="text-xs text-amber-300">{policyRejections[0]?.detail ?? 'A policy gate rejected this plan.'}</p>
							{/if}
						</section>
					{/if}

					<section class="space-y-2">
						<h3 class="text-xs font-semibold uppercase tracking-wide text-dark-300">Files</h3>
						{#if preview.files.length}
							<ul class="space-y-1 text-xs">
								{#each preview.files as file}
									{@const filePath = typeof file === 'string' ? file : file?.path ?? file?.file_path ?? 'Unknown file'}
									{@const fileOperation = typeof file === 'string' ? '' : file?.operation ?? ''}
									<li class="flex flex-wrap items-center gap-2 text-dark-200">
										<span class="break-all">{filePath}</span>
										{#if fileOperation}
											<span class="rounded-sm bg-dark-700 px-1.5 py-0.5 text-[10px] text-dark-400">{fileOperation}</span>
										{/if}
									</li>
								{/each}
							</ul>
						{:else}
							<p class="text-xs text-dark-500">No files were returned.</p>
						{/if}
					</section>

					<section class="space-y-1">
						<h3 class="text-xs font-semibold uppercase tracking-wide text-dark-300">Source diff</h3>
						<pre class="text-[11px] leading-relaxed text-dark-300 bg-dark-950 border border-dark-700 rounded-lg p-3 overflow-x-auto whitespace-pre">{preview.sourceDiff || 'No source diff was returned.'}</pre>
					</section>

					<section class="space-y-1">
						<h3 class="text-xs font-semibold uppercase tracking-wide text-dark-300">Rendered diff</h3>
						<pre class="text-[11px] leading-relaxed text-dark-300 bg-dark-950 border border-dark-700 rounded-lg p-3 overflow-x-auto whitespace-pre">{preview.renderedDiff || 'No rendered diff was returned.'}</pre>
					</section>

					<section class="space-y-2">
						<h3 class="text-xs font-semibold uppercase tracking-wide text-dark-300">Validation</h3>
						<p class="text-xs {preview.validationPassed ? 'text-green-400' : 'text-red-400'}">
							{preview.validationPassed ? 'Validation passed' : 'Validation did not pass'}
						</p>
						<ul class="space-y-1 text-xs text-dark-400">
							{#if preview.validation.target_changed != null}
								<li>Target changed: {preview.validation.target_changed ? 'yes' : 'no'}</li>
							{/if}
							{#if preview.validation.unrelated_changes != null}
								<li>Unrelated changes: {preview.validation.unrelated_changes ? 'yes' : 'no'}</li>
							{/if}
							{#if preview.validation.requests_within_limits != null}
								<li>Requests within limits: {preview.validation.requests_within_limits ? 'yes' : 'no'}</li>
							{/if}
						</ul>
						{#if preview.validation.reason}
							<p class="text-xs text-dark-400">{preview.validation.reason}</p>
						{/if}
						{#if preview.validation.output_digest}
							<p class="text-xs text-dark-500 break-all">
								Rendered output digest: <span class="text-dark-300">{preview.validation.output_digest}</span>
							</p>
						{/if}
					</section>

					<section class="space-y-2">
						<h3 class="text-xs font-semibold uppercase tracking-wide text-dark-300">Warnings</h3>
						{#if preview.warnings.length}
							<ul class="list-disc pl-5 space-y-1 text-xs text-yellow-400">
								{#each preview.warnings as warning}
									<li>{warning}</li>
								{/each}
							</ul>
						{:else}
							<p class="text-xs text-dark-500">No API-reported warnings.</p>
						{/if}
					</section>

					<section class="grid grid-cols-1 sm:grid-cols-2 gap-4 border-t border-dark-700 pt-3">
						<div>
							<p class="text-xs text-dark-500">Digest</p>
							<p class="text-xs text-dark-200 break-all">{preview.digest || 'Not provided'}</p>
						</div>
						<div>
							<p class="text-xs text-dark-500">Expires</p>
							<p class="text-xs text-dark-200 break-all">{preview.expiresAt || 'Not provided'}</p>
						</div>
					</section>
				</div>
			{/if}

			<div class="flex gap-3 justify-end pt-1">
				<button class="btn-secondary text-xs" on:click={closePrModal}>Close</button>
				{#if canWrite && canCancelPlan(prPlan)}
					<button
						class="btn-secondary text-xs disabled:opacity-50"
						disabled={prLoading}
						on:click={cancelPlan}
					>
						{prLoading ? 'Cancelling…' : 'Cancel plan'}
					</button>
				{/if}
				{#if canWrite && canReplan(prPlan)}
					<button
						class="btn-secondary text-xs disabled:opacity-50"
						disabled={prLoading}
						on:click={rePlan}
					>
						Re-plan
					</button>
				{/if}
				{#if canWrite && canPreviewRollback(prPlan)}
					<button
						class="btn-secondary text-xs disabled:opacity-50"
						disabled={prLoading}
						on:click={previewPlanRollback}
					>
						{prLoading ? 'Preparing rollback…' : 'Preview rollback'}
					</button>
				{/if}
				{#if prPlan && canApprove && prPlan?.may_approve !== false && canApprovePlan(prPlan) && !planApprovalBlocked && !planApproval}
					<button
						class="btn-primary text-xs flex items-center gap-2 disabled:opacity-50"
						disabled={prLoading}
						on:click={approvePlan}
					>
						{#if prLoading}<span class="animate-spin">⟳</span>{/if}
						{approveLabel()}
					</button>
				{:else if prPlan && (!canApprove || prPlan?.may_approve === false) && canApprovePlan(prPlan) && !planApprovalBlocked && !planApproval}
					<p class="self-center text-xs text-dark-400" data-testid="approve-read-only">
						Approving a plan needs the approve scope (operator or admin).
					</p>
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
			<p class="text-sm text-dark-500 mt-1">Ranked, evidence-backed changes, ready as pull requests</p>
		</div>
		<div class="flex items-center gap-3 flex-wrap">
			<label class="flex items-center gap-2 text-xs text-dark-400">
				<span>Sort by</span>
				<select
					aria-label="Sort recommendations"
					class="bg-dark-800 border border-dark-600 rounded-lg px-2 py-1.5 text-xs text-dark-200"
					value={sortMode}
					on:change={(e) => changeSort(e.currentTarget.value)}
					title="Expected value is the server order: projected value (cost and carbon) times the success probability, minus incident and effort costs. Profiles re-score every recommendation matching the filters."
				>
					{#each SORT_OPTIONS as option}
						<option value={option.value}>{option.label}</option>
					{/each}
				</select>
			</label>
			{#if canWrite}
				<button
					class="btn-secondary text-xs disabled:opacity-50"
					disabled={scanPending}
					title="Run a new recommendation scan and reload the results when it completes"
					on:click={rescan}
				>
					{scanPending ? '⟳ Scanning…' : '↻ Rescan'}
				</button>
			{/if}
		</div>
	</div>

	{#if !canWrite}
		<p class="rounded-lg border border-dark-700 bg-dark-900 px-3 py-2 text-xs text-dark-400" role="note" data-testid="read-only-hint">
			Read-only access{$currentUser?.role ? ` (${$currentUser.role})` : ''}: you can review recommendations and their
			evidence. Opening pull requests, ignoring, restoring and rescanning need an operator or admin.
		</p>
	{/if}

	{#if scanError}
		<div class="card text-sm text-amber-300" role="alert">{scanError}</div>
	{:else if scanRun}
		<div
			class="card text-sm {scanRun.status === 'failed' ? 'text-amber-300' : 'text-dark-300'}"
			role={scanRun.status === 'failed' ? 'alert' : 'status'}
		>
			{#if scanPending}<span class="animate-spin inline-block mr-1">⟳</span>{/if}
			{scanStatusText(scanRun)}
		</div>
	{/if}

	<!-- Filters (sent to the API as query parameters) -->
	<div class="flex flex-wrap items-end gap-3" role="group" aria-label="Recommendation filters">
		<label class="flex flex-col gap-1 text-[10px] uppercase tracking-wide text-dark-500">
			Namespace
			<select
				aria-label="Filter by namespace"
				class="bg-dark-800 border border-dark-600 rounded-lg px-2 py-1.5 text-xs normal-case tracking-normal text-dark-200"
				bind:value={$selectedNamespace}
			>
				<option value="">All namespaces</option>
				{#each namespaceOptions as ns}
					<option value={ns}>{ns}</option>
				{/each}
				{#if $selectedNamespace && !namespaceOptions.includes($selectedNamespace)}
					<option value={$selectedNamespace}>{$selectedNamespace}</option>
				{/if}
			</select>
		</label>
		<label class="flex flex-col gap-1 text-[10px] uppercase tracking-wide text-dark-500">
			Source
			<select
				aria-label="Filter by source"
				class="bg-dark-800 border border-dark-600 rounded-lg px-2 py-1.5 text-xs normal-case tracking-normal text-dark-200"
				bind:value={filterSource}
			>
				<option value="">All sources</option>
				{#each sourceOptions as source}
					<option value={source}>{getSourceConfig(source).label}</option>
				{/each}
			</select>
		</label>
		<label class="flex flex-col gap-1 text-[10px] uppercase tracking-wide text-dark-500">
			Risk
			<select
				aria-label="Filter by risk level"
				class="bg-dark-800 border border-dark-600 rounded-lg px-2 py-1.5 text-xs normal-case tracking-normal text-dark-200"
				bind:value={filterRisk}
			>
				<option value="">Any risk</option>
				{#each RISK_OPTIONS as risk}
					<option value={risk.value}>{risk.label}</option>
				{/each}
			</select>
		</label>
		<label class="flex flex-col gap-1 text-[10px] uppercase tracking-wide text-dark-500">
			Capability
			<select
				aria-label="Filter by capability"
				class="bg-dark-800 border border-dark-600 rounded-lg px-2 py-1.5 text-xs normal-case tracking-normal text-dark-200"
				bind:value={filterCapability}
			>
				<option value="">All capabilities</option>
				{#each CAPABILITY_OPTIONS as capability}
					<option value={capability.value}>{capability.label}</option>
				{/each}
			</select>
		</label>
		{#if serverFiltersActive || $selectedNamespace}
			<button class="btn-secondary text-xs" on:click={clearFilters}>Clear filters ✕</button>
		{/if}
	</div>

	{#if $demoMode}
		<div class="card text-sm text-dark-300" role="status" data-testid="demo-notice">
			Demo data: pull requests are disabled in the demo. Install GreenKube in your cluster to open them.
		</div>
	{:else if automationStatusError || (automationStatus === null && !loading)}
		<div class="card text-sm text-amber-300" role="alert">
			GitOps automation status is unavailable. Pull-request previews are disabled; check the Automation page and API health.
		</div>
	{:else if automationStatus === null}
		<div class="card text-sm text-dark-400" role="status">
			Checking GitOps automation readiness…
		</div>
	{:else if automationStatus.enabled !== true}
		<div class="card text-sm text-dark-400" role="status">
			GitOps automation is disabled. Recommendations remain visible, but pull-request previews are unavailable; apply changes manually.
		</div>
	{:else if automationStatus.ready !== true}
		<div class="card text-sm text-amber-300" role="status">
			GitOps automation is enabled but not ready. Pull-request previews are disabled until its worker is healthy.
		</div>
	{/if}

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
				{actionableActiveRecs.length}
			</span>
		</button>
		<!-- Pull requests live on the changes board (K4): a link, not a second list. -->
		<a
			class="px-4 py-2 rounded-lg text-sm font-medium transition-colors text-dark-500 hover:text-dark-300"
			href="/changes"
			title="Pull requests and their sync, apply and measurement stages are on the Changes board"
			data-testid="changes-link"
		>
			Pull requests
			<span class="ml-1.5 text-xs px-1.5 py-0.5 rounded-full bg-dark-700 text-dark-500">
				{openPullRequests.length}
			</span>
			<span aria-hidden="true">→</span>
		</a>
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
			💰 Measured savings
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
						<div class="card-compact text-center" data-testid="savings-tab-co2e">
							<p class="stat-label">{SAVINGS_TERMS.allocatedCo2e}</p>
							<p class="stat-value text-2xl text-green-400">{formatCO2(savingsTabFigures.measuredAllocatedToDate?.co2e_grams ?? savings.measured_carbon_saved_co2e_grams ?? 0)}</p>
							<p class="text-[10px] text-dark-600 uppercase tracking-wide mt-1">
								measured, to date · projected separately {formatCO2(savingsTabFigures.projectedToDate?.co2e_grams ?? savings.prorated_carbon_saved_co2e_grams ?? 0)}
							</p>
						</div>
						<div class="card-compact text-center" data-testid="savings-tab-cost">
							<p class="stat-label">{SAVINGS_TERMS.measuredAllocated}</p>
							<p class="stat-value text-2xl text-blue-400">{formatCost(savingsTabFigures.measuredAllocatedToDate?.cost ?? savings.measured_cost_saved ?? 0)}</p>
							<p class="text-[10px] text-dark-600 uppercase tracking-wide mt-1">
								measured, to date · projected separately {formatCost(savingsTabFigures.projectedToDate?.cost ?? savings.prorated_cost_saved ?? 0)}
							</p>
						</div>
					</div>
					{#if realizationText(savingsTabFigures.realizationFactor)}
						<p class="text-xs text-dark-400" data-testid="savings-tab-realization">{realizationText(savingsTabFigures.realizationFactor)}</p>
					{/if}
					{#if savingsTabFigures.realizedBilledToDate}
						<p class="text-xs text-green-300" data-testid="savings-tab-billed">
							{SAVINGS_TERMS.realizedBilled}: {formatCost(savingsTabFigures.realizedBilledToDate.cost ?? 0)} · {formatCO2(savingsTabFigures.realizedBilledToDate.co2e_grams ?? 0)} CO₂e
						</p>
					{/if}

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
										<div class="text-xl shrink-0">{cfg.icon}</div>
										<div class="flex-1 min-w-0">
											<div class="flex items-center gap-2 flex-wrap">
												<span class="text-sm font-semibold text-dark-100">
													{rec.pod_name ?? rec.target_node ?? rec.namespace ?? 'Cluster-wide'}
												</span>
												<a
													href="/recommendations/{rec.id}"
													class="text-[10px] text-green-400 hover:text-green-300"
													aria-label="Details of recommendation {rec.id}"
													on:click|stopPropagation
												>Details</a>
												<span class="badge-{cfg.color} text-[10px]">{cfg.label}</span>
												{#if rec.status === 'verified'}
													<span class="text-[10px] px-2 py-0.5 rounded-sm bg-green-600/20 text-green-400">✓ verified</span>
												{:else if rec.status === 'rollback_review'}
													<span class="text-[10px] px-2 py-0.5 rounded-sm bg-red-600/20 text-red-400">rollback review</span>
												{:else if rec.status === 'verifying'}
													<span class="text-[10px] px-2 py-0.5 rounded-sm bg-blue-600/20 text-blue-400">verifying…</span>
												{:else}
													<span class="text-[10px] px-2 py-0.5 rounded-sm bg-blue-600/10 text-blue-400">applied</span>
												{/if}
												{#if rec.verification_status}
													<span class="text-[10px] px-2 py-0.5 rounded-sm {verificationClass(rec.verification_status)}">
														{rec.verification_status}
													</span>
												{/if}
											</div>
											{#if rec.namespace}
												<p class="text-xs text-dark-500 mt-0.5">Namespace: <span class="text-dark-400">{rec.namespace}</span></p>
											{/if}
											{#each requestChanges(rec) as change (change.resource)}
												{#if change.after != null && !change.unchanged}
													<p class="text-xs text-dark-500 mt-0.5 font-mono" data-testid="request-summary-{change.resource}">
														{change.label} {change.format(change.before)} → <span class="text-blue-400">{change.format(change.after)}</span>
													</p>
												{:else if change.after == null && change.proposed != null}
													<!-- Live value after the change unknown (never detected, or cleared by migration 0061). -->
													<p class="text-xs text-dark-500 mt-0.5 font-mono" data-testid="request-summary-{change.resource}">
														{change.label} {change.format(change.before)} → <span class="text-green-400">{change.format(change.proposed)}</span> <span class="font-sans text-[10px] text-dark-600">(proposed)</span>
													</p>
												{/if}
											{/each}
										</div>
										<!-- Savings chips -->
										<div class="flex items-center gap-3 shrink-0">
											{#if rec.carbon_saved_co2e_grams}
												<span class="text-xs font-semibold text-green-400">{savingPhrase(rec.carbon_saved_co2e_grams, formatCO2, { verb: 'saved', suffix: ' CO₂e/yr' })}</span>
											{/if}
											{#if rec.cost_saved}
												<span class="text-xs text-blue-400">{savingPhrase(rec.cost_saved, formatCost, { verb: 'saved' })}</span>
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
													class="text-[10px] px-2 py-0.5 rounded-sm border border-dark-600 text-dark-400
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

											{#if requestChanges(rec).length}
												<div class="flex flex-col gap-1.5">
													{#each requestChanges(rec) as change (change.resource)}
														<div class="flex flex-wrap items-center gap-2" data-testid="request-change-{change.resource}">
															<span class="text-[10px] uppercase text-dark-600 w-14">{change.label}</span>
															<span class="text-xs text-dark-400 font-mono" title="Request before the change">{change.format(change.before)}</span>
															<span class="text-dark-600" aria-hidden="true">→</span>
															{#if change.after != null && !change.unchanged}
																<span class="text-xs text-blue-400 font-mono" title="Live request detected after the change">{change.format(change.after)}</span>
																<span class="text-[10px] text-dark-600">(live after the change)</span>
																{#if change.proposed != null && change.proposed !== change.after}
																	<span class="text-[10px] text-dark-600">· proposed {change.format(change.proposed)}</span>
																{/if}
															{:else if change.proposed != null}
																<span class="text-xs text-green-400 font-mono" title="Proposed request">{change.format(change.proposed)}</span>
																<span class="text-[10px] text-dark-600">(proposed)</span>
															{/if}
															{#if change.unchanged}
																<span class="text-[10px] px-2 py-0.5 rounded-sm bg-yellow-600/20 text-yellow-400" title="The live request detected after the change still equals the request before it.">
																	live request unchanged ({change.format(change.after)})
																</span>
															{/if}
														</div>
													{/each}
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
			{#if activeTab === 'active' && actionableActiveRecs.length}
				<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
					<div class="card-compact text-center">
						<p class="stat-label">{serverFiltersActive ? 'Matching Recommendations' : 'Active Recommendations'}</p>
						<p class="stat-value text-2xl">{actionableActiveRecs.length}</p>
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
						{#if opportunity?.reliabilityCost}
							<p class="text-[10px] text-dark-500 mt-1" data-testid="opportunity-reliability-cost">
								Reliability fixes cost {formatCost(opportunity.reliabilityCost)}/yr (not counted)
							</p>
						{/if}
					</div>
				</div>
			{/if}

			{#if activeTab === 'ignored' && ignoredRecs.length === 0}
				<div class="card text-center py-12">
					<p class="text-4xl mb-3">👀</p>
					<p class="text-dark-400 text-sm">No ignored recommendations.</p>
				</div>
			{:else if activeTab === 'ignored' && currentList.length === 0}
				<div class="card text-center py-12">
					<p class="text-dark-400 text-sm">No ignored recommendations match these filters.</p>
				</div>
			{:else if activeTab === 'active' && actionableActiveRecs.length === 0 && serverFiltersActive}
				<div class="card text-center py-12">
					<p class="text-dark-400 text-sm">No active recommendations match these filters.</p>
				</div>
			{:else if activeTab === 'active' && actionableActiveRecs.length === 0}
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
						{@const topFactor = topRankingFactor(rec.ranking_factors)}
						{@const evText = describeExpectedValue(rec)}
						{@const eligibility = eligibilityOf(rec)}
						{@const diagnostics = visibleDiagnostics(eligibility, $demoMode)}
						<div class="card hover:border-{cfg.color}-600/30 transition-all duration-200">
							<div class="flex items-start gap-4">
								<div class="text-2xl shrink-0 mt-0.5">{cfg.icon}</div>
								<div class="flex-1 min-w-0">
									<!-- Top row: title + actions -->
									<div class="flex items-start justify-between gap-3 flex-wrap">
										<div class="min-w-0">
											<div class="flex items-center gap-2 flex-wrap">
												<h3 class="text-sm font-semibold text-dark-100">
													<a href="/recommendations/{rec.id}" class="hover:text-green-300" data-testid="recommendation-detail-link">
														{rec.pod_name ?? rec.target_node ?? rec.namespace ?? 'Cluster-wide'}
													</a>
												</h3>
												<span class="badge-{cfg.color} text-[10px]">{cfg.label}</span>
												{#if hasSafePerContainerAction(rec) && automationStatus?.enabled === true && automationStatus?.ready === true}
													<span class="text-[10px] px-2 py-0.5 rounded-sm bg-green-600/10 text-green-400">Container action ready</span>
												{:else if activeTab === 'active'}
													<span
														class="text-[10px] px-2 py-0.5 rounded-sm bg-dark-700 text-dark-400"
														title={manualReason(rec)}
													>Manual change required</span>
												{/if}
												{#if activeTab === 'active' && warmUpText(rec)}
													<span
														class="text-[10px] px-2 py-0.5 rounded-sm bg-blue-600/10 text-blue-300"
														role="status"
														data-testid="warm-up-eta"
													>{warmUpText(rec)}</span>
												{/if}
												{#if isReliabilityLane(rec)}
													<span
														class="text-[10px] px-2 py-0.5 rounded-sm bg-purple-600/10 text-purple-300"
														title="Increases resources to reduce incident risk; it costs money."
													>Reliability</span>
												{/if}
												<span class="text-[10px] px-2 py-0.5 rounded-sm bg-dark-700 text-dark-300">
													{getSourceConfig(rec.source).label}
												</span>
												{#if rec.risk_level}
													<span class="text-[10px] px-2 py-0.5 rounded-sm {riskClass(rec.risk_level)}"
														title={rec.risk_factors?.join(', ')}>
														{rec.risk_level} risk
													</span>
												{/if}
												{#if evText}
													<span
														class="text-[10px] px-2 py-0.5 rounded-sm bg-dark-700 text-dark-200"
														title={describeFactorsInCurrency(rec) || 'No ranking breakdown was returned.'}
														data-testid="ranking-score"
													>
														{evText}{topFactor ? ` · top factor: ${topFactor.label}` : ''}
													</span>
												{:else if rec.ranking_score != null}
													<span
														class="text-[10px] px-2 py-0.5 rounded-sm bg-dark-700 text-dark-200"
														title={describeRankingFactors(rec.ranking_factors) || 'No ranking breakdown was returned.'}
														data-testid="ranking-score"
													>
														Score {formatRankingScore(rec.ranking_score)}{topFactor ? ` · top factor: ${topFactor.label}` : ''}
													</span>
												{/if}
												{#if activeTab === 'ignored'}
													<span class="text-[10px] px-2 py-0.5 rounded-sm bg-yellow-600/10 text-yellow-500">ignored</span>
												{/if}
											</div>
											{#if rec.namespace}
												<p class="text-xs text-dark-500 mt-0.5">Namespace: <span class="text-dark-400">{rec.namespace}</span></p>
											{/if}
										</div>
										<div class="flex items-center gap-3 shrink-0">
											{#if rec.potential_savings_co2e_grams || rec.potential_savings_cost}
												<div class="text-right">
													{#if rec.potential_savings_co2e_grams}
														<p class="text-sm font-bold text-green-400">{savingPhrase(rec.potential_savings_co2e_grams, formatCO2, { suffix: ' CO₂e/yr' })}</p>
													{/if}
													{#if rec.potential_savings_cost && rec.projected_billable_cost_annual === null}
														<p class="text-xs text-blue-400" data-testid="row-allocated-unknown">allocated: {savingPhrase(rec.potential_savings_cost, formatCost)}</p>
													{:else if rec.potential_savings_cost}
														<p class="text-xs text-blue-400">{savingPhrase(rec.potential_savings_cost, formatCost)}</p>
													{/if}
													{#if rec.projection_note}<p class="text-[10px] text-dark-500" data-testid="row-projection-note">{rec.projection_note}</p>{/if}
												</div>
											{:else if rec.projected_allocated_cost_annual > 0}
												<!-- IMP-03: on fixed nodes the billable projection is 0; show the allocated figure, never hide it. -->
												<div class="text-right" data-testid="row-allocated-saving">
													<p class="text-xs text-blue-400">allocated: {savingPhrase(rec.projected_allocated_cost_annual, formatCost)}</p>
													{#if rec.projection_note}<p class="text-[10px] text-dark-500">{rec.projection_note}</p>{/if}
												</div>
											{/if}
											{#if capacitySummary(rec.released_capacity)}
												<p class="text-[11px] text-dark-400 text-right" data-testid="row-released-capacity" title="Reserved capacity, not money: never added to cost savings">
													{capacitySummary(rec.released_capacity)}
												</p>
											{/if}
											{#if activeTab === 'active'}
												{#if canWrite && canPreviewRecommendation(rec)}
													<button
														class="text-xs px-3 py-1.5 rounded-lg border border-dark-600 text-dark-400
														       hover:border-green-600/50 hover:text-green-400 transition-colors"
														title="Preview the diff of the server-resolved GitOps plan, then open the pull request in one click"
														on:click={() => openPrModal(rec)}
													>
														Open PR
													</button>
												{:else if rec.scope === 'workload' && rec.owner_kind && rec.owner_name && hasOpenPr(rec)}
													<button
														class="text-xs px-3 py-1.5 rounded-lg border border-blue-600/30 text-blue-400/70
														       cursor-not-allowed"
														title="A pull request is already open for this workload recommendation"
														disabled
													>PR created</button>
												{/if}
												<a
													class="text-xs px-3 py-1.5 rounded-lg border border-dark-600 text-dark-400
													       hover:border-green-600/50 hover:text-green-400 transition-colors"
													title="kubectl steps for clusters without GitOps, then mark the recommendation as applied"
													href="/recommendations/{rec.id}#manual"
													data-testid="apply-manually"
												>Apply manually</a>
												{#if canWrite}
													<button
														class="text-xs px-3 py-1.5 rounded-lg border border-dark-600 text-dark-400
														       hover:border-yellow-600/50 hover:text-yellow-400 transition-colors"
														title="Ignore this recommendation"
														on:click={() => openIgnoreModal(rec)}
													>Ignore</button>
												{/if}
											{:else if canWrite}
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
									{#if activeTab === 'ignored' && rec.changed_since_ignored}
										<div class="mt-2 text-xs text-orange-300/90 bg-orange-600/5 border border-orange-600/20 rounded-lg px-3 py-2">
											Advice changed since you ignored it{rec.changed_since_ignored.reason ? `: ${rec.changed_since_ignored.reason}` : '.'}
										</div>
									{/if}

									<!-- Description / reason -->
									<p class="text-sm text-dark-400 mt-2">{rec.reason}</p>

									<!-- Server eligibility (F10): why no pull request, and how to fix it -->
									{#if activeTab === 'active' && eligibility && (diagnostics.blockers.length || diagnostics.warnings.length)}
										<ul class="mt-2 space-y-1 text-xs" data-testid="eligibility-blockers">
											{#each diagnostics.blockers as blocker (blocker.code + blocker.message)}
												<li class="text-dark-400">
													<span class="text-amber-300">{blocker.message}</span>
													{#if blocker.fix}<span class="text-dark-500"> — {blocker.fix}</span>{/if}
												</li>
											{/each}
											{#each diagnostics.warnings.filter((entry) => entry.code !== 'oom_history_unknown') as warning (warning.code + warning.message)}
												<li class="text-dark-500">
													<span class="text-yellow-500/80">⚠ {warning.message}</span>
													{#if warning.fix}<span> — {warning.fix}</span>{/if}
												</li>
											{/each}
										</ul>
										<OomHistoryWarning warning={oomHistoryWarningOf(rec)} />
									{/if}

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
											<code class="text-xs text-indigo-400 bg-dark-800 px-2 py-0.5 rounded-sm font-mono">{rec.cron_schedule}</code>
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
											<span class="text-[10px] uppercase px-2 py-0.5 rounded-sm
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
										{#if rec.evidence}
											<button
												class="text-[10px] px-2 py-0.5 rounded-sm border border-dark-600 text-dark-400
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
