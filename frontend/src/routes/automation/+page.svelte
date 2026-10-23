<script>
	import { onMount } from 'svelte';
	import DataState from '$lib/components/DataState.svelte';
	import {
		getAutomationStatus,
		getGitOpsBindings,
		getGitOpsCoverage,
		getGitOpsStatus,
		getOpenPullRequests,
		refreshGitOpsDiscovery,
		resolveGitOpsBinding
	} from '$lib/api.js';

	const CAPABILITIES = [
		{ key: 'argocd', label: 'Argo CD' },
		{ key: 'flux_kustomization', label: 'Flux Kustomization' },
		{ key: 'flux_helm_release', label: 'Flux HelmRelease' },
		{ key: 'flux_source_git_repository', label: 'Flux GitRepository' },
		{ key: 'flux_source_oci_repository', label: 'Flux OCIRepository' },
		{ key: 'flux_source_helm_repository', label: 'Flux HelmRepository' },
		{ key: 'flux_source_bucket', label: 'Flux Bucket' }
	];

	const PROVIDER_LABELS = {
		github: 'GitHub',
		gitlab: 'GitLab',
		gitea: 'Gitea'
	};

	const SYNC_STATUS_LABELS = {
		synced: 'Synced',
		pending: 'Pending',
		failed: 'Failed',
		unavailable: 'Unavailable',
		mismatched: 'Revision mismatch'
	};

	const SYNC_ERROR_LABELS = {
		sync_confirmed: 'Sync confirmed',
		reconciliation_pending: 'Reconciliation pending',
		controller_failed: 'Controller failed',
		controller_not_found: 'Controller not found',
		controller_ambiguous: 'Controller ambiguous',
		controller_identity_mismatch: 'Controller identity mismatch',
		controller_not_ready: 'Controller not ready',
		source_unresolved: 'Source unresolved',
		source_mismatch: 'Source mismatch',
		source_ambiguous: 'Source ambiguous',
		unsupported_controller: 'Unsupported controller',
		revision_unavailable: 'Revision unavailable',
		revision_mismatch: 'Revision mismatch',
		invalid_response: 'Invalid response',
		response_too_large: 'Response too large',
		response_truncated: 'Response truncated'
	};

	const SAFE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
	const SAFE_REVISION =
		/^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|sha1:[0-9a-fA-F]{40}|sha256:[0-9a-fA-F]{64})$/;
	const SAFE_REPOSITORY_SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$/;
	const SAFE_GENERATION_TOKEN = /^sha256:[0-9a-f]{64}$/;
	const SAFE_REPOSITORY_HOST = /^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$/;
	const SAFE_PATH_SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/;
	const BINDING_REASON_LABELS = {
		'all-source-candidates-stale': 'All current source candidates are stale',
		'ambiguous-multi-source-owner': 'Multiple source owners were found',
		'conflicting-equal-priority-source-units': 'Multiple current sources disagree',
		'conflicting-source-forms': 'Source configuration is ambiguous',
		'highest-priority-source-is-ambiguous': 'The highest-priority source is ambiguous',
		'highest-priority-source-blocked': 'The highest-priority source is blocked',
		'highest-priority-source-unresolved': 'The highest-priority source is unresolved',
		'manual-resolution-expired': 'The manual source resolution expired',
		'manual-resolution-expiry-missing': 'The manual source resolution has no expiry',
		'manual-resolution-source-changed': 'The manually resolved source changed',
		'provider-unresolved': 'No configured provider matches this source',
		'source-ref-ambiguous': 'The source reference matches multiple repositories',
		'source-expired': 'The source observation expired',
		'source-not-observed-in-refresh': 'The source was not observed in the latest discovery refresh',
		'source-observation-expired': 'The source observation expired',
		'source-ref-unresolved': 'The referenced Git source is unavailable',
		'source-repository-unresolved': 'The repository URL is unavailable',
		'workload-not-observed-in-refresh': 'The workload was not observed in the latest discovery refresh',
		'workload-source-unresolved': 'No source could be matched to this workload'
	};

	let loading = true;
	let refreshingDiscovery = false;
	let pageError = '';
	let discoveryRefreshError = '';
	let discoveryRefreshSuccess = '';
	let discoveryStatus = null;
	let coverage = null;
	let bindings = [];
	let providerReadiness = null;
	let pullRequests = [];
	let discoveryError = false;
	let coverageError = false;
	let bindingsError = false;
	let providerReadinessError = false;
	let pullRequestsError = false;
	let selectedCandidates = {};
	let confirmations = {};
	let resolvingBindings = {};
	let resolutionErrors = {};
	let resolutionSuccess = '';
	let loadSequence = 0;
	let bindingUpdateSequence = 0;

	onMount(() => {
		loadOperations();
	});

	function isRecord(value) {
		return value !== null && typeof value === 'object' && !Array.isArray(value);
	}

	function safeIdentifier(value) {
		return typeof value === 'string' && SAFE_IDENTIFIER.test(value) ? value : '—';
	}

	function controllerLabel(value) {
		return typeof value === 'string' &&
			value.length <= 256 &&
			/^[A-Za-z0-9._/-]+$/.test(value)
			? value
			: 'Controller omitted';
	}

	function providerLabel(value) {
		if (typeof value !== 'string') return 'Unrecognized provider';
		return PROVIDER_LABELS[value.trim().toLowerCase()] ?? 'Unrecognized provider';
	}

	function repositoryLabel(value) {
		if (typeof value !== 'string' || value.length > 400) return '—';
		const segments = value.split('/');
		if (
			segments.length < 2 ||
			segments.length > 5 ||
			!segments.every((segment) => segment !== '.' && segment !== '..' && SAFE_REPOSITORY_SEGMENT.test(segment))
		) {
			return '—';
		}
		return value;
	}

	function sourceRepositoryLabel(value) {
		if (typeof value !== 'string' || value.length > 512) return '—';
		if (!/^[A-Za-z][A-Za-z0-9+.-]*:\/\//.test(value)) {
			const scpRepository = /^([A-Za-z0-9][A-Za-z0-9.-]{0,252}):([A-Za-z0-9_./-]{1,512})$/.exec(
				value
			);
			if (scpRepository) {
				const segments = scpRepository[2].split('/');
				return segments.every(
					(segment) =>
						segment &&
						segment !== '.' &&
						segment !== '..' &&
						SAFE_REPOSITORY_SEGMENT.test(segment)
				)
					? `${scpRepository[1]}:${scpRepository[2]}`
					: '—';
			}
		}
		try {
			const repository = new URL(value);
			if (!['https:', 'http:', 'ssh:', 'git:'].includes(repository.protocol)) return '—';
			const host = repository.hostname;
			const port = repository.port ? `:${repository.port}` : '';
			const path = repository.pathname.replace(/^\/+|\/+$/g, '');
			const segments = path ? path.split('/') : [];
			if (
				!SAFE_REPOSITORY_HOST.test(host) ||
				segments.length > 5 ||
				!segments.every(
					(segment) =>
						segment !== '.' &&
						segment !== '..' &&
						SAFE_REPOSITORY_SEGMENT.test(segment)
				)
			) {
				return '—';
			}
			return `${host}${port}${segments.length ? `/${segments.join('/')}` : ''}`;
		} catch {
			return repositoryLabel(value);
		}
	}

	function safeRepositoryPath(value) {
		if (typeof value !== 'string' || value.length > 512) return '—';
		const segments = value.split('/');
		return segments.length <= 20 &&
			segments.every(
				(segment) =>
					segment !== '.' &&
					segment !== '..' &&
					SAFE_PATH_SEGMENT.test(segment)
			)
			? value
			: '—';
	}

	function revisionLabel(value) {
		return typeof value === 'string' && SAFE_REVISION.test(value) ? value : '—';
	}

	function timestampLabel(value) {
		if (typeof value !== 'string') return '—';
		const timestamp = new Date(value);
		return Number.isNaN(timestamp.getTime()) ? '—' : timestamp.toISOString();
	}

	function countLabel(value) {
		return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : '—';
	}

	function ratioLabel(value) {
		return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1
			? `${Math.round(value * 100)}%`
			: '—';
	}

	function capabilityLabel(capability) {
		if (!isRecord(capability) || typeof capability.available !== 'boolean') return 'Unknown';
		return capability.available ? 'Available' : 'Unavailable';
	}

	function capabilityTone(capability) {
		if (!isRecord(capability) || typeof capability.available !== 'boolean') {
			return 'bg-dark-800 text-dark-300';
		}
		return capability.available
			? 'bg-green-500/10 text-green-400'
			: 'bg-yellow-500/10 text-yellow-300';
	}

	function capabilityNote(capability) {
		if (!isRecord(capability)) return '';
		if (capability.reason === 'crd-not-found') return 'CRD not detected';
		if (typeof capability.reason === 'string' && capability.reason) return 'Reason omitted';
		return '';
	}

	function planReadiness(status) {
		if (!isRecord(status)) return 'Readiness unknown';
		if (status.ready === true) return 'GitOps plan automation is ready';
		if (status.enabled === true && !isRecord(status.worker)) return 'GitOps plan automation is ready';
		if (status.enabled === true) return 'GitOps plan automation is enabled; worker heartbeat is stale';
		if (status.enabled === false) return 'GitOps plan automation is not configured';
		return 'Readiness unknown';
	}

	function automationMetric(value) {
		return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? String(value) : '—';
	}

	function syncStatusLabel(value) {
		if (typeof value !== 'string') return '—';
		return SYNC_STATUS_LABELS[value.trim().toLowerCase()] ?? '—';
	}

	function syncErrorLabel(value) {
		if (typeof value !== 'string') return '—';
		return SYNC_ERROR_LABELS[value.trim().toLowerCase()] ?? '—';
	}

	function bindingReasonLabel(value) {
		if (typeof value !== 'string') return '';
		return BINDING_REASON_LABELS[value] ?? 'Reason omitted';
	}

	function bindingStatusLabel(status) {
		const labels = {
			ambiguous: 'Ambiguous',
			unresolved: 'Unresolved',
			blocked: 'Blocked',
			stale: 'Stale'
		};
		return labels[status] ?? 'Unknown';
	}

	function bindingStatusTone(status) {
		if (status === 'blocked' || status === 'stale') {
			return 'bg-red-500/10 text-red-300';
		}
		return 'bg-yellow-500/10 text-yellow-300';
	}

	function projectBinding(record) {
		if (!isRecord(record)) return null;
		const status = typeof record.status === 'string' ? record.status.trim().toLowerCase() : '';
		if (!['ambiguous', 'unresolved', 'blocked', 'stale'].includes(status)) return null;
		const namespace = safeIdentifier(record.namespace);
		const workloadKind = safeIdentifier(record.workload_kind);
		const workloadName = safeIdentifier(record.workload_name);
		if (namespace === '—' || workloadKind === '—' || workloadName === '—') return null;

		const candidates = Array.isArray(record.candidates)
			? record.candidates
					.filter((candidate) => {
						if (!isRecord(candidate)) return false;
						const candidateStatus =
							typeof candidate.status === 'string' ? candidate.status.trim().toLowerCase() : '';
						return (
							candidateStatus === 'resolved' ||
							candidateStatus === 'ambiguous'
						);
					})
					.map((candidate) => {
						const id = safeIdentifier(candidate.id);
						const generationToken =
							typeof candidate.generation_token === 'string' &&
							SAFE_GENERATION_TOKEN.test(candidate.generation_token)
								? candidate.generation_token
								: '';
						if (id === '—' || !generationToken || candidate.controller_kind === 'manual_resolution') {
							return null;
						}
						return {
							id,
							generationToken,
							controller: controllerLabel(candidate.controller),
							providerId: safeIdentifier(candidate.provider_id),
							repository: sourceRepositoryLabel(candidate.repo_url),
							path: safeRepositoryPath(candidate.path),
							renderer: safeIdentifier(candidate.renderer),
							status: candidate.status,
							reason: bindingReasonLabel(candidate.reason),
							revision: revisionLabel(candidate.revision)
						};
					})
					.filter((candidate) => candidate !== null)
			: [];

		return {
			key: `${namespace}/${workloadKind}/${workloadName}`,
			namespace,
			workloadKind,
			workloadName,
			status,
			statusLabel: bindingStatusLabel(status),
			statusTone: bindingStatusTone(status),
			canResolve: status === 'ambiguous' || status === 'unresolved',
			reason: bindingReasonLabel(record.reason),
			candidates,
			candidatesTruncated: record.candidates_truncated === true
		};
	}

	function bindingCandidateSignature(binding) {
		const candidates = binding.candidates
			.map((candidate) => [
				candidate.id,
				candidate.generationToken,
				candidate.controller,
				candidate.providerId,
				candidate.repository,
				candidate.path,
				candidate.renderer,
				candidate.status,
				candidate.reason,
				candidate.revision
			])
			.sort((left, right) =>
				String(left[0]).localeCompare(String(right[0])) ||
				String(left[1]).localeCompare(String(right[1]))
			);
		return JSON.stringify([binding.status, binding.candidatesTruncated, candidates]);
	}

	function clearResolutionState(keys) {
		const nextSelectedCandidates = { ...selectedCandidates };
		const nextConfirmations = { ...confirmations };
		const nextResolutionErrors = { ...resolutionErrors };
		for (const key of keys) {
			delete nextSelectedCandidates[key];
			delete nextConfirmations[key];
			delete nextResolutionErrors[key];
		}
		selectedCandidates = nextSelectedCandidates;
		confirmations = nextConfirmations;
		resolutionErrors = nextResolutionErrors;
	}

	function updateBindings(records) {
		const nextBindings = Array.isArray(records)
			? records.map((record) => projectBinding(record)).filter((record) => record !== null)
			: [];
		const previousByKey = new Map(bindings.map((binding) => [binding.key, binding]));
		const nextByKey = new Map(nextBindings.map((binding) => [binding.key, binding]));
		const changedKeys = new Set([...previousByKey.keys(), ...nextByKey.keys()]);

		for (const key of changedKeys) {
			const previous = previousByKey.get(key);
			const next = nextByKey.get(key);
			if (
				!previous ||
				!next ||
				bindingCandidateSignature(previous) !== bindingCandidateSignature(next)
			) {
				continue;
			}
			changedKeys.delete(key);
		}

		clearResolutionState(changedKeys);
		bindings = nextBindings;
	}

	function chooseCandidate(bindingKey, candidateId) {
		selectedCandidates = { ...selectedCandidates, [bindingKey]: candidateId };
		confirmations = { ...confirmations, [bindingKey]: false };
		resolutionErrors = { ...resolutionErrors, [bindingKey]: '' };
		resolutionSuccess = '';
	}

	function confirmBinding(bindingKey, confirmed) {
		confirmations = { ...confirmations, [bindingKey]: confirmed === true };
	}

	async function resolveBinding(binding) {
		if (loading) return;
		const candidate = binding.candidates.find(
			(item) => item.id === selectedCandidates[binding.key]
		);
		if (!candidate || confirmations[binding.key] !== true) return;

		resolutionErrors = { ...resolutionErrors, [binding.key]: '' };
		resolutionSuccess = '';
		resolvingBindings = { ...resolvingBindings, [binding.key]: true };
		try {
			await resolveGitOpsBinding({
				namespace: binding.namespace,
				workload_kind: binding.workloadKind,
				workload_name: binding.workloadName,
				candidate_id: candidate.id,
				generation_token: candidate.generationToken,
				confirmed: true
			});
			resolutionSuccess = 'Manual binding resolution saved.';
			selectedCandidates = { ...selectedCandidates, [binding.key]: '' };
			confirmations = { ...confirmations, [binding.key]: false };
			const updateSequence = ++bindingUpdateSequence;
			const [coverageResult, bindingsResult] = await Promise.allSettled([
				getGitOpsCoverage(),
				getGitOpsBindings()
			]);
			if (updateSequence !== bindingUpdateSequence) return;
			if (coverageResult.status === 'fulfilled' && isRecord(coverageResult.value)) {
				coverage = coverageResult.value;
				coverageError = false;
			} else {
				coverageError = true;
			}
			if (bindingsResult.status === 'fulfilled' && isRecord(bindingsResult.value)) {
				updateBindings(bindingsResult.value.items);
				bindingsError = false;
			} else {
				bindingsError = true;
				clearResolutionState(
					new Set([
						...Object.keys(selectedCandidates),
						...Object.keys(confirmations),
						...bindings.map((item) => item.key)
					])
				);
			}
		} catch {
			resolutionErrors = {
				...resolutionErrors,
				[binding.key]: 'Binding resolution failed. Refresh candidates and try again.'
			};
		} finally {
			resolvingBindings = { ...resolvingBindings, [binding.key]: false };
		}
	}

	function projectPullRequest(record, index) {
		if (!isRecord(record)) return null;
		const normalizedStatus = typeof record.status === 'string' ? record.status.trim().toLowerCase() : '';
		if (normalizedStatus !== 'pending' && normalizedStatus !== 'open') return null;

		return {
			key: `pull-request-${index}`,
			planId: safeIdentifier(record.plan_id),
			provider: providerLabel(record.provider),
			providerId: safeIdentifier(record.provider_id),
			repo: repositoryLabel(record.repo),
			status: normalizedStatus === 'pending' ? 'Pending' : 'Open',
			syncStatus: syncStatusLabel(record.sync_status),
			syncRevision: revisionLabel(record.sync_revision),
			syncObservedAt: timestampLabel(record.sync_observed_at),
			syncNote: syncErrorLabel(record.sync_error_code)
		};
	}

	async function refreshDiscovery() {
		if (loading || refreshingDiscovery) return;
		refreshingDiscovery = true;
		discoveryRefreshError = '';
		discoveryRefreshSuccess = '';
		try {
			await refreshGitOpsDiscovery();
			discoveryRefreshSuccess = 'Discovery refresh completed.';
		} catch (error) {
			discoveryRefreshError = error?.message || 'GitOps discovery refresh failed.';
		}
		await loadOperations();
		refreshingDiscovery = false;
	}

	async function loadOperations() {
		const sequence = ++loadSequence;
		const bindingSequence = ++bindingUpdateSequence;
		loading = true;
		pageError = '';
		discoveryStatus = null;
		coverage = null;
		providerReadiness = null;
		pullRequests = [];
		discoveryError = true;
		coverageError = true;
		bindingsError = true;
		providerReadinessError = true;
		pullRequestsError = true;
		resolutionErrors = {};
		resolutionSuccess = '';

		const [statusResult, coverageResult, bindingsResult, providerResult, pullRequestsResult] = await Promise.allSettled([
			getGitOpsStatus(),
			getGitOpsCoverage(),
			getGitOpsBindings(),
			getAutomationStatus(),
			getOpenPullRequests()
		]);

		if (sequence !== loadSequence) return;

		if (statusResult.status === 'fulfilled' && isRecord(statusResult.value)) {
			discoveryStatus = statusResult.value;
			discoveryError = !isRecord(statusResult.value.discovery);
		}

		if (coverageResult.status === 'fulfilled' && isRecord(coverageResult.value)) {
			coverage = coverageResult.value;
		} else if (isRecord(discoveryStatus?.coverage)) {
			coverage = discoveryStatus.coverage;
		}
		coverageError = !coverage;

		if (bindingsResult.status === 'fulfilled' && isRecord(bindingsResult.value)) {
			if (bindingSequence === bindingUpdateSequence) {
				updateBindings(bindingsResult.value.items);
				bindingsError = false;
			}
		} else if (bindingSequence === bindingUpdateSequence) {
			bindingsError = true;
			clearResolutionState(
				new Set([
					...Object.keys(selectedCandidates),
					...Object.keys(confirmations),
					...bindings.map((item) => item.key)
				])
			);
		}

		if (providerResult.status === 'fulfilled' && isRecord(providerResult.value)) {
			providerReadiness = providerResult.value;
			providerReadinessError = false;
		}

		if (pullRequestsResult.status === 'fulfilled' && Array.isArray(pullRequestsResult.value)) {
			pullRequests = pullRequestsResult.value
				.map((record, index) => projectPullRequest(record, index))
				.filter((record) => record !== null);
			pullRequestsError = false;
		}

		const hasData =
			!discoveryError || !coverageError || !bindingsError || !providerReadinessError || !pullRequestsError;
		if (!hasData) pageError = 'Automation operations could not be loaded.';
		if (sequence === loadSequence) loading = false;
	}
</script>

<div class="mx-auto max-w-[1600px] space-y-6 p-6 lg:p-8">
	<header class="flex items-start justify-between gap-4">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Automation &amp; GitOps</h1>
			<p class="mt-1 text-sm text-dark-500">
				Review workload source candidates, approve expiring manual bindings, and inspect pull-request operations.
			</p>
		</div>
		<button
			type="button"
			class="rounded-lg border border-dark-700 px-3 py-2 text-sm text-dark-300 hover:bg-dark-800 disabled:opacity-50"
			disabled={loading || refreshingDiscovery}
			on:click={refreshDiscovery}
		>
			{loading || refreshingDiscovery ? 'Refreshing…' : 'Refresh discovery & operations'}
		</button>
	</header>

	<DataState
		{loading}
		error={pageError}
		canRetry={Boolean(pageError)}
		on:retry={loadOperations}
	>
		<div class="space-y-6">
			<section class="rounded-2xl border border-dark-700/50 bg-dark-900/70 p-5">
				<div class="mb-4">
					<h2 class="text-lg font-semibold text-dark-100">GitOps discovery</h2>
					<p class="mt-1 text-sm text-dark-500">
						Controller availability, workload bindings, and current discovery coverage.
					</p>
					{#if discoveryRefreshError}
						<p class="mt-2 text-sm text-red-400">{discoveryRefreshError}</p>
					{:else if discoveryRefreshSuccess}
						<p class="mt-2 text-sm text-green-400">{discoveryRefreshSuccess}</p>
					{/if}
				</div>

				{#if discoveryError}
					<p class="text-sm text-red-400" role="alert">GitOps discovery status is unavailable.</p>
				{:else}
					<div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
						{#each CAPABILITIES as capability}
							{@const state = discoveryStatus?.discovery?.[capability.key]}
							<div class="rounded-xl border border-dark-700/50 bg-dark-950/40 p-4">
								<div class="flex items-start justify-between gap-3">
									<p class="text-sm font-medium text-dark-200">{capability.label}</p>
									<span class="rounded-full px-2 py-1 text-xs {capabilityTone(state)}">
										{capabilityLabel(state)}
									</span>
								</div>
								{#if capabilityNote(state)}
									<p class="mt-2 text-xs text-dark-500">{capabilityNote(state)}</p>
								{/if}
							</div>
						{/each}
					</div>
					<p class="mt-4 text-xs text-dark-500">
						Last discovery refresh: {timestampLabel(discoveryStatus?.last_refresh)}
					</p>
				{/if}
			</section>

			<section class="rounded-2xl border border-dark-700/50 bg-dark-900/70 p-5">
				<div class="mb-4">
					<h2 class="text-lg font-semibold text-dark-100">Workload binding issues and manual resolution</h2>
					<p class="mt-1 text-sm text-dark-500">
						Review ambiguous, unresolved, blocked, and stale workload sources. Reload to read the latest
						persisted discovery snapshot; only ambiguous and unresolved bindings can be manually resolved.
					</p>
				</div>

				{#if resolutionSuccess}
					<p class="mb-4 text-sm text-green-400" role="status">{resolutionSuccess}</p>
				{/if}

				{#if bindingsError}
					<p class="text-sm text-red-400" role="alert">
						Actionable workload bindings are unavailable.
					</p>
				{:else if bindings.length === 0}
					<p class="text-sm text-dark-400">
						No ambiguous, unresolved, blocked, or stale workload bindings were returned.
					</p>
				{:else}
					<div class="space-y-3">
						{#each bindings as binding (binding.key)}
							<article class="rounded-xl border border-dark-700/50 bg-dark-950/40 p-4">
								<div class="mb-3 flex flex-wrap items-center justify-between gap-3">
									<h3 class="font-medium text-dark-100">
										{binding.workloadKind} {binding.namespace}/{binding.workloadName}
									</h3>
									<span class={`rounded-full px-2.5 py-1 text-xs font-medium ${binding.statusTone}`}>
										{binding.statusLabel}
									</span>
								</div>
								{#if binding.reason}
									<p class="mb-3 text-sm text-dark-400">{binding.reason}</p>
								{/if}
								{#if binding.candidates.length === 0}
									<p class="text-sm text-dark-400">
										No current source candidate can be selected. Correct the source configuration and wait for the next discovery update.
									</p>
								{:else}
									<ul class="mb-4 space-y-2">
										{#each binding.candidates as candidate (candidate.id)}
											<li class="rounded-lg border border-dark-800 p-3 text-sm">
												<p class="font-medium text-dark-200">{candidate.controller}</p>
												<p class="mt-1 break-all text-dark-400">
													{candidate.providerId} · {candidate.repository} · {candidate.path} · {candidate.renderer}
												</p>
												{#if candidate.reason}
													<p class="mt-1 text-xs text-dark-500">Reason: {candidate.reason}</p>
												{/if}
											</li>
										{/each}
									</ul>
									{#if binding.candidatesTruncated}
										<p class="mb-3 text-xs text-yellow-300">
											Only the first available source candidates are shown. Refresh the binding before resolving.
										</p>
									{/if}

									{#if binding.canResolve}
										<div class="space-y-3">
											<label
												for={`candidate-${binding.key}`}
												class="block text-sm font-medium text-dark-300"
											>
												Candidate source for {binding.key}
											</label>
											<select
												id={`candidate-${binding.key}`}
												aria-label={`Candidate source for ${binding.key}`}
												class="w-full rounded-lg border border-dark-700 bg-dark-950 px-3 py-2 text-sm text-dark-200"
												value={selectedCandidates[binding.key] ?? ''}
												on:change={(event) => chooseCandidate(binding.key, event.currentTarget.value)}
											>
												<option value="">Select a current source</option>
												{#each binding.candidates as candidate (candidate.id)}
													<option value={candidate.id}>
														{candidate.controller} — {candidate.repository} — {candidate.path}
													</option>
												{/each}
											</select>

											<label class="flex items-start gap-2 text-sm text-dark-300">
												<input
													type="checkbox"
													class="mt-1"
													checked={confirmations[binding.key] === true}
													on:change={(event) =>
														confirmBinding(binding.key, event.currentTarget.checked)}
												/>
												<span>I confirm this source is the correct GitOps owner</span>
											</label>

											<button
												type="button"
												class="rounded-lg bg-primary-600 px-3 py-2 text-sm font-medium text-white hover:bg-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
												disabled={
													!selectedCandidates[binding.key] ||
													confirmations[binding.key] !== true ||
													loading ||
													resolvingBindings[binding.key] === true
												}
												on:click={() => resolveBinding(binding)}
											>
												{resolvingBindings[binding.key] ? 'Resolving…' : 'Resolve binding'}
											</button>
											{#if resolutionErrors[binding.key]}
												<p class="text-sm text-red-400" role="alert">
													{resolutionErrors[binding.key]}
												</p>
											{/if}
										</div>
									{:else}
										<p class="text-sm text-dark-400">
											Manual resolution is unavailable while this binding is {binding.statusLabel.toLowerCase()}.
											Correct the source configuration and wait for the next discovery update.
										</p>
									{/if}
								{/if}
							</article>
						{/each}
					</div>
				{/if}
			</section>

			<section class="rounded-2xl border border-dark-700/50 bg-dark-900/70 p-5">
				<div class="mb-4">
					<h2 class="text-lg font-semibold text-dark-100">Workload coverage</h2>
					<p class="mt-1 text-sm text-dark-500">
						Persisted workload binding counts for the selected cluster.
					</p>
				</div>

				{#if coverageError}
					<p class="text-sm text-red-400" role="alert">Workload coverage is unavailable.</p>
				{:else}
					{#if countLabel(coverage.total) === '0'}
						<p class="mb-4 text-sm text-dark-400">No workload bindings have been observed yet.</p>
					{/if}
					<dl class="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-7">
						{#each [
							{ label: 'Total', value: countLabel(coverage.total) },
							{ label: 'Resolved', value: countLabel(coverage.resolved) },
							{ label: 'Ambiguous', value: countLabel(coverage.ambiguous) },
							{ label: 'Unresolved', value: countLabel(coverage.unresolved) },
							{ label: 'Blocked', value: countLabel(coverage.blocked) },
							{ label: 'Stale', value: countLabel(coverage.stale) },
							{ label: 'Coverage', value: ratioLabel(coverage.ratio) }
						] as metric}
							<div class="rounded-xl border border-dark-700/50 bg-dark-950/40 p-4">
								<dt class="text-xs uppercase tracking-wide text-dark-500">{metric.label}</dt>
								<dd class="mt-2 text-xl font-semibold text-dark-100">{metric.value}</dd>
							</div>
						{/each}
					</dl>
				{/if}
			</section>

			<section class="rounded-2xl border border-dark-700/50 bg-dark-900/70 p-5">
				<div class="mb-4">
					<h2 class="text-lg font-semibold text-dark-100">GitOps plan automation readiness</h2>
					<p class="mt-1 text-sm text-dark-500">
						Provider Secrets are mounted read-only; secret values are never displayed.
					</p>
				</div>

				{#if providerReadinessError}
					<p class="text-sm text-red-400" role="alert">GitOps plan automation readiness is unavailable.</p>
				{:else}
					<div class="flex flex-wrap items-center gap-3">
						<span class="rounded-full bg-dark-800 px-3 py-1.5 text-sm text-dark-200">
							{planReadiness(providerReadiness)}
						</span>
						<span class="text-sm text-dark-400">
							Provider registry: {providerReadiness.provider === 'gitops' ? 'Configured' : 'Unavailable'}
						</span>
					</div>
					<dl class="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
						<div class="rounded-xl border border-dark-700/50 bg-dark-950/40 p-4">
							<dt class="text-xs uppercase tracking-wide text-dark-500">Queued operations</dt>
							<dd class="mt-2 text-lg font-semibold text-dark-100">{automationMetric(providerReadiness.queue?.queued)}</dd>
						</div>
						<div class="rounded-xl border border-dark-700/50 bg-dark-950/40 p-4">
							<dt class="text-xs uppercase tracking-wide text-dark-500">Stuck operations</dt>
							<dd class="mt-2 text-lg font-semibold text-dark-100">{automationMetric(providerReadiness.queue?.stuck)}</dd>
						</div>
						<div class="rounded-xl border border-dark-700/50 bg-dark-950/40 p-4">
							<dt class="text-xs uppercase tracking-wide text-dark-500">Worker heartbeat</dt>
							<dd class="mt-2 text-sm font-semibold text-dark-100">
								{providerReadiness.worker?.healthy === true ? 'Healthy' : 'Stale or absent'}
							</dd>
						</div>
						<div class="rounded-xl border border-dark-700/50 bg-dark-950/40 p-4">
							<dt class="text-xs uppercase tracking-wide text-dark-500">Discovery age (s)</dt>
							<dd class="mt-2 text-lg font-semibold text-dark-100">{automationMetric(providerReadiness.discovery?.age_seconds)}</dd>
						</div>
					</dl>
				{/if}
			</section>

			<section class="rounded-2xl border border-dark-700/50 bg-dark-900/70 p-5">
				<div class="mb-4">
					<h2 class="text-lg font-semibold text-dark-100">Pending and open pull requests</h2>
					<p class="mt-1 text-sm text-dark-500">
						Read-only records with safe plan, provider, repository, and sync metadata.
					</p>
				</div>

				{#if pullRequestsError}
					<p class="text-sm text-red-400" role="alert">Pull-request records are unavailable.</p>
				{:else if pullRequests.length === 0}
					<p class="text-sm text-dark-400">No pending or open pull requests.</p>
				{:else}
					<div class="space-y-3">
						{#each pullRequests as pullRequest (pullRequest.key)}
							<article class="rounded-xl border border-dark-700/50 bg-dark-950/40 p-4">
								<div class="mb-4 flex flex-wrap items-center justify-between gap-3">
									<h3 class="font-medium text-dark-100">Pull request</h3>
									<span class="rounded-full bg-blue-500/10 px-2.5 py-1 text-xs font-medium text-blue-300">
										{pullRequest.status}
									</span>
								</div>
								<dl class="grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-2 xl:grid-cols-3">
									<div>
										<dt class="text-xs text-dark-500">Plan</dt>
										<dd class="mt-1 break-all font-mono text-dark-200">{pullRequest.planId}</dd>
									</div>
									<div>
										<dt class="text-xs text-dark-500">Provider</dt>
										<dd class="mt-1 text-dark-200">{pullRequest.provider}</dd>
									</div>
									<div>
										<dt class="text-xs text-dark-500">Provider ID</dt>
										<dd class="mt-1 break-all font-mono text-dark-200">{pullRequest.providerId}</dd>
									</div>
									<div>
										<dt class="text-xs text-dark-500">Repository</dt>
										<dd class="mt-1 break-all font-mono text-dark-200">{pullRequest.repo}</dd>
									</div>
									<div>
										<dt class="text-xs text-dark-500">Sync status</dt>
										<dd class="mt-1 text-dark-200">{pullRequest.syncStatus}</dd>
									</div>
									<div>
										<dt class="text-xs text-dark-500">Sync revision</dt>
										<dd class="mt-1 break-all font-mono text-dark-200">{pullRequest.syncRevision}</dd>
									</div>
									<div>
										<dt class="text-xs text-dark-500">Sync observed</dt>
										<dd class="mt-1 text-dark-200">{pullRequest.syncObservedAt}</dd>
									</div>
									<div>
										<dt class="text-xs text-dark-500">Sync note</dt>
										<dd class="mt-1 text-dark-200">{pullRequest.syncNote}</dd>
									</div>
								</dl>
							</article>
						{/each}
					</div>
				{/if}
			</section>
		</div>
	</DataState>
</div>
