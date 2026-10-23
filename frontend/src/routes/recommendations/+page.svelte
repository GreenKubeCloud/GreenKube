<script>
	import { selectedNamespace } from '$lib/stores.js';
	import { onDestroy } from 'svelte';
	import {
		getActiveRecommendations,
		getIgnoredRecommendations,
		getAppliedRecommendations,
		getRecommendationSavings,
		getOpenPullRequests,
		getTopRecommendations,
		ignoreRecommendation,
		unignoreRecommendation,
		createAutomationPlan,
		getAutomationPlan,
		getAutomationOperation,
		approveAutomationPlan,
		cancelAutomationPlan,
		previewAutomationPlanRollback,
		getRecommendationEvents
	} from '$lib/api.js';
	import { formatCO2, formatCost, formatCPU, formatBytes } from '$lib/utils/format.js';
	import { groupConsecutiveEvents } from '$lib/utils/lifecycle.js';
	import DataState from '$lib/components/DataState.svelte';

	// --- State ---
	let activeRecs = [];
	let ignoredRecs = [];
	let appliedRecs = [];
	let openPullRequests = [];
	let openPullRequestKeys = new Set();
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

	const PLAN_POLL_INTERVAL_MS = 3000;

	// Verification event trail per applied recommendation
	let eventsByRec = {}; // { [id]: Event[] }

	// Per-card action loading state
	let actionLoading = {}; // { [id]: bool }

	$: if ($selectedNamespace !== undefined) loadData();

	let loadSeq = 0;
	let requestController = new AbortController();
	onDestroy(() => {
		requestController.abort();
		stopPlanStatusPolling();
	});

	async function loadData() {
		// Guard against overlapping loads: a slower response must not overwrite
		// the result of a newer request.
		const seq = ++loadSeq;
		requestController.abort();
		requestController = new AbortController();
		loading = true;
		error = null;
		try {
			const [active, ignored, applied, sav, openPrs] = await Promise.all([
				getActiveRecommendations({ namespace: $selectedNamespace || undefined, refresh: true }),
				getIgnoredRecommendations(),
				getAppliedRecommendations(),
				getRecommendationSavings({ namespace: $selectedNamespace || undefined }),
				getOpenPullRequests()
			]);
			if (seq !== loadSeq) return;
			activeRecs = active;
			ignoredRecs = ignored;
			appliedRecs = applied;
			savings = sav;
			openPullRequests = openPrs;
			const openPrIds = new Set(openPrs.map((pr) => pr.recommendation_id));
			openPullRequestKeys = new Set(
				active.filter((rec) => openPrIds.has(rec.id)).map((rec) => recommendationKey(rec))
			);
		} catch (e) {
			if (seq === loadSeq && e.name !== 'AbortError') error = e.message;
		} finally {
			if (seq === loadSeq) loading = false;
		}

	}

	// --- Type filter ---
	$: openPrRecommendationIds = new Set(openPullRequests.map((pr) => pr.recommendation_id));
	$: actionableActiveRecs = activeRecs;
	$: currentList = activeTab === 'active' ? actionableActiveRecs : ignoredRecs;
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

	$: totalSavingsCO2 = actionableActiveRecs.reduce((s, r) => s + (r.potential_savings_co2e_grams ?? 0), 0);
	$: totalSavingsCost = actionableActiveRecs.reduce((s, r) => s + (r.potential_savings_cost ?? 0), 0);
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

	// --- GitOps plan preview ---
	const RIGHTSIZING_TYPES = ['RIGHTSIZING_CPU', 'RIGHTSIZING_MEMORY'];

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

	function safeActionFor(rec) {
		const action = rec?.action ?? rec?.patch?.action;
		const id = Number(rec?.id);
		const target = action?.target;
		const recommendationIds = Array.isArray(action?.recommendation_ids)
			? action.recommendation_ids.map(Number)
			: [];
		if (
			!Number.isInteger(id) ||
			id <= 0 ||
			action?.version !== 3 ||
			!action.action_id ||
			!recommendationIds.length ||
			!recommendationIds.every((recommendationId) => Number.isInteger(recommendationId) && recommendationId > 0) ||
			new Set(recommendationIds).size !== recommendationIds.length ||
			!recommendationIds.includes(id) ||
			action.constraints?.container_targeted !== true ||
			rec.scope !== 'workload' ||
			!rec.owner_kind ||
			!rec.owner_name ||
			!rec.namespace ||
			!rec.container_name ||
			!target?.api_version ||
			target?.kind !== rec.owner_kind ||
			target?.name !== rec.owner_name ||
			target?.namespace !== rec.namespace ||
			!Array.isArray(action.operations) ||
			!action.operations.length
		) {
			return null;
		}

		const containers = action.operations.map((operation) => operation?.container);
		if (
			new Set(containers).size !== containers.length ||
			!action.operations.some((operation) => operation?.container === rec.container_name) ||
			action.operations.some((operation) => {
				const requests = operation?.resources?.requests;
				return (
					operation?.op !== 'set_container_resources' ||
					typeof operation.container !== 'string' ||
					!operation.container.trim() ||
					!requests ||
					typeof requests !== 'object' ||
					Array.isArray(requests) ||
					!Object.keys(requests).length ||
					Object.entries(requests).some(
						([resource, quantity]) =>
							!['cpu', 'memory'].includes(resource) ||
							typeof quantity !== 'string' ||
							!quantity.trim()
					)
				);
			})
		) {
			return null;
		}

		return { action, recommendationIds };
	}

	function hasSafePerContainerAction(rec) {
		return Boolean(safeActionFor(rec));
	}

	function canPreviewRecommendation(rec) {
		return Boolean(safeActionFor(rec)) && !hasOpenPr(rec);
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
		plan_preparation_unavailable: 'Server-side plan preparation is unavailable for this recommendation.'
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
			/\b(plan_preview_unavailable|plan_preparation_unavailable|unsupported_source|needs_chart_mapping|ambiguous_binding|plan_stale|source_changed|plan_expired|revision_conflict|provider_not_ready|provider_capability_missing|requires_container_target|rollback_not_ready|rollback_unsupported|rollback_conflict)\b/i
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
					['stale', 'failed', 'closed', 'superseded', 'rejected'].includes(preview.status) ||
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
				const response = preview.operationId
					? await getAutomationOperation(preview.operationId)
					: await getAutomationPlan(preview.planId);
				if (generation !== planPollGeneration || prModal !== modal) return;
				prPlan = preview.operationId
					? {
							...prPlan,
							operation_id: response?.operation_id ?? preview.operationId,
							operation_status: response?.status ?? prPlan?.operation_status,
							status: response?.result?.plan_status ?? prPlan?.status
						}
					: response?.plan ?? response;
				planPollingError = '';
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
			if (prModal !== modal) return;
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

	function closePrModal() {
		stopPlanStatusPolling();
		prModal = null;
		prPlan = null;
		planApproval = null;
		planApprovalBlocked = false;
		planFeedback = '';
		planPollingError = '';
		prError = null;
		prLoading = false;
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
			planApprovalBlocked = String(prPlan?.status ?? '').toLowerCase() === 'stale';
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
			if (prModal !== modal) return;
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
			superseded: 'Plan superseded'
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
				{#if canCancelPlan(prPlan)}
					<button
						class="btn-secondary text-xs disabled:opacity-50"
						disabled={prLoading}
						on:click={cancelPlan}
					>
						{prLoading ? 'Cancelling…' : 'Cancel plan'}
					</button>
				{/if}
				{#if canReplan(prPlan)}
					<button
						class="btn-secondary text-xs disabled:opacity-50"
						disabled={prLoading}
						on:click={rePlan}
					>
						Re-plan
					</button>
				{/if}
				{#if canPreviewRollback(prPlan)}
					<button
						class="btn-secondary text-xs disabled:opacity-50"
						disabled={prLoading}
						on:click={previewPlanRollback}
					>
						{prLoading ? 'Preparing rollback…' : 'Preview rollback'}
					</button>
				{/if}
				{#if prPlan && canApprovePlan(prPlan) && !planApprovalBlocked && !planApproval}
					<button
						class="btn-primary text-xs flex items-center gap-2 disabled:opacity-50"
						disabled={prLoading}
						on:click={approvePlan}
					>
						{#if prLoading}<span class="animate-spin">⟳</span>{/if}
						Approve plan
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
				{actionableActiveRecs.length}
			</span>
		</button>
		<button
			class="px-4 py-2 rounded-lg text-sm font-medium transition-colors
			       {activeTab === 'pull-requests' ? 'bg-dark-700 text-dark-100' : 'text-dark-500 hover:text-dark-300'}"
			on:click={() => switchTab('pull-requests')}
		>
			Pull requests
			<span class="ml-1.5 text-xs px-1.5 py-0.5 rounded-full
			             {activeTab === 'pull-requests' ? 'bg-blue-600/20 text-blue-400' : 'bg-dark-700 text-dark-500'}">
				{openPullRequests.length}
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
										<div class="text-xl shrink-0">{cfg.icon}</div>
										<div class="flex-1 min-w-0">
											<div class="flex items-center gap-2 flex-wrap">
												<span class="text-sm font-semibold text-dark-100">
													{rec.pod_name ?? rec.target_node ?? rec.namespace ?? 'Cluster-wide'}
												</span>
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
										</div>
										<!-- Savings chips -->
										<div class="flex items-center gap-3 shrink-0">
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

		{:else if activeTab === 'pull-requests'}
			<div class="space-y-3">
				<div>
					<h2 class="text-sm font-semibold text-dark-300">Open pull requests</h2>
					<p class="text-xs text-dark-500 mt-1">
						Recommendations with a pending or open pull request are kept here until the PR is closed.
					</p>
				</div>
				{#if openPullRequests.length}
					<div class="space-y-2">
						{#each openPullRequests as pr (pr.id)}
							<div class="card flex flex-col sm:flex-row sm:items-center gap-3">
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2 flex-wrap">
										<span class="text-sm font-semibold text-dark-100">Recommendation #{pr.recommendation_id}</span>
										<span class="text-[10px] px-2 py-0.5 rounded-sm bg-blue-600/20 text-blue-400">{pr.status}</span>
									</div>
									<p class="text-xs text-dark-500 mt-1">
										{pr.provider} · {pr.repo} · {pr.head_branch}
									</p>
								</div>
								{#if pr.pr_url}
									<a class="text-xs text-blue-400 hover:text-blue-300 underline break-all" href={pr.pr_url} target="_blank" rel="noreferrer">
										View PR
									</a>
								{/if}
							</div>
						{/each}
					</div>
				{:else}
					<div class="card text-sm text-dark-500">No pending or open pull requests.</div>
				{/if}
			</div>

		<!-- ═══ ACTIVE / IGNORED TABS ════════════════════════════════════════════ -->
		{:else}
			<!-- Potential savings summary (active only) -->
			{#if activeTab === 'active' && actionableActiveRecs.length}
				<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
					<div class="card-compact text-center">
						<p class="stat-label">Active Recommendations</p>
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
					</div>
				</div>
			{/if}

			{#if activeTab === 'ignored' && ignoredRecs.length === 0}
				<div class="card text-center py-12">
					<p class="text-4xl mb-3">👀</p>
					<p class="text-dark-400 text-sm">No ignored recommendations.</p>
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
						<div class="card hover:border-{cfg.color}-600/30 transition-all duration-200">
							<div class="flex items-start gap-4">
								<div class="text-2xl shrink-0 mt-0.5">{cfg.icon}</div>
								<div class="flex-1 min-w-0">
									<!-- Top row: title + actions -->
									<div class="flex items-start justify-between gap-3 flex-wrap">
										<div class="min-w-0">
											<div class="flex items-center gap-2 flex-wrap">
												<h3 class="text-sm font-semibold text-dark-100">
													{rec.pod_name ?? rec.target_node ?? rec.namespace ?? 'Cluster-wide'}
												</h3>
												<span class="badge-{cfg.color} text-[10px]">{cfg.label}</span>
												{#if hasSafePerContainerAction(rec)}
													<span class="text-[10px] px-2 py-0.5 rounded-sm bg-green-600/10 text-green-400">Container action ready</span>
												{:else if RIGHTSIZING_TYPES.includes(rec.type)}
													<span class="text-[10px] px-2 py-0.5 rounded-sm bg-dark-700 text-dark-500">No safe container action</span>
												{:else}
													<span class="text-[10px] px-2 py-0.5 rounded-sm bg-dark-700 text-dark-400" title="This recommendation has no GitOps action planner; apply it manually.">Manual change required</span>
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
														<p class="text-sm font-bold text-green-400">-{formatCO2(rec.potential_savings_co2e_grams)}</p>
													{/if}
													{#if rec.potential_savings_cost}
														<p class="text-xs text-blue-400">-{formatCost(rec.potential_savings_cost)}</p>
													{/if}
												</div>
											{/if}
											{#if activeTab === 'active'}
												{#if canPreviewRecommendation(rec)}
													<button
														class="text-xs px-3 py-1.5 rounded-lg border border-dark-600 text-dark-400
														       hover:border-green-600/50 hover:text-green-400 transition-colors"
														title="Preview the server-resolved GitOps plan for this container action"
														on:click={() => openPrModal(rec)}
													>
														Preview plan
													</button>
												{:else if rec.scope === 'workload' && rec.owner_kind && rec.owner_name && hasOpenPr(rec)}
													<button
														class="text-xs px-3 py-1.5 rounded-lg border border-blue-600/30 text-blue-400/70
														       cursor-not-allowed"
														title="A pull request is already open for this workload recommendation"
														disabled
													>PR created</button>
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
										{#if rec.ranking_score != null}
											<span class="text-[10px] text-dark-500" title={JSON.stringify(rec.ranking_factors ?? {})}>
												rank score {rec.ranking_score.toFixed(2)}
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
