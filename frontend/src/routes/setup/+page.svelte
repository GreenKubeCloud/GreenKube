<script>
	import { onDestroy, onMount } from 'svelte';
	import { ApiError, allowlistRepositories, getRepositorySuggestions, getSetupStatus } from '$lib/api.js';
	import { currentUser, hasScope } from '$lib/stores.js';
	import {
		CHECK_STATUS,
		allowLabel,
		capabilityDisplay,
		groupChecks,
		normalizeAllowlistResult,
		normalizeSetupStatus,
		normalizeSuggestions,
		setupStatus
	} from '$lib/setup.js';
	import StatusPill from '$lib/components/StatusPill.svelte';

	let status = null;
	let statusError = '';
	let statusUnsupported = false;
	let loading = true;
	let suggestions = [];
	let suggestionsError = '';
	let suggestionsUnsupported = false;
	let selectedUrls = new Set();
	let allowing = false;
	let allowError = '';
	let allowMessage = '';
	let allowSnippet = '';
	let copied = '';
	let controller = new AbortController();

	onMount(load);
	onDestroy(() => controller.abort());

	async function load() {
		controller.abort();
		controller = new AbortController();
		const { signal } = controller;
		loading = true;
		const [statusResult, suggestionsResult] = await Promise.allSettled([
			getSetupStatus({ signal }),
			getRepositorySuggestions({ signal })
		]);
		if (signal.aborted) return;
		statusUnsupported = false;
		statusError = '';
		if (statusResult.status === 'fulfilled') {
			status = normalizeSetupStatus(statusResult.value);
			if (!status) statusError = 'The server returned an unexpected setup status.';
			setupStatus.set(status);
		} else if (statusResult.reason instanceof ApiError && statusResult.reason.status === 404) {
			statusUnsupported = true;
			status = null;
		} else {
			statusError = statusResult.reason?.message || 'The setup status is unavailable.';
		}
		suggestionsUnsupported = false;
		suggestionsError = '';
		if (suggestionsResult.status === 'fulfilled') {
			suggestions = normalizeSuggestions(suggestionsResult.value);
			selectedUrls = new Set(suggestions.filter((item) => !item.allowlisted).map((item) => item.repoUrl));
		} else if (suggestionsResult.reason instanceof ApiError && suggestionsResult.reason.status === 404) {
			suggestionsUnsupported = true;
			suggestions = [];
		} else {
			suggestionsError = suggestionsResult.reason?.message || 'Repository suggestions are unavailable.';
		}
		loading = false;
	}

	function toggle(url) {
		const next = new Set(selectedUrls);
		if (next.has(url)) next.delete(url);
		else next.add(url);
		selectedUrls = next;
	}

	async function allowSelected() {
		const urls = pending.filter((item) => selectedUrls.has(item.repoUrl)).map((item) => item.repoUrl);
		if (!urls.length || allowing) return;
		allowing = true;
		allowError = '';
		allowMessage = '';
		allowSnippet = '';
		try {
			// The server allows them in its database and returns the equivalent
			// automation.repositoryAllowlist values snippet for GitOps-managed installs.
			const result = normalizeAllowlistResult(await allowlistRepositories(urls), urls.length);
			allowSnippet = result.snippet;
			allowMessage = result.message;
			await load();
		} catch (error) {
			allowError =
				error instanceof ApiError && error.status === 403
					? 'Only an administrator can change the repository allowlist.'
					: error?.message || 'The allowlist could not be updated.';
		} finally {
			allowing = false;
		}
	}

	async function copy(id, snippet) {
		try {
			await navigator.clipboard.writeText(snippet);
			copied = id;
			setTimeout(() => {
				if (copied === id) copied = '';
			}, 2000);
		} catch {
			copied = '';
		}
	}

	$: groups = groupChecks(status?.checks ?? []);
	$: pending = suggestions.filter((item) => !item.allowlisted);
	$: allowlisted = suggestions.filter((item) => item.allowlisted);
	$: selectedCount = pending.filter((item) => selectedUrls.has(item.repoUrl)).length;
	$: isAdmin = hasScope($currentUser, 'admin');
</script>

<svelte:head><title>Setup · GreenKube</title></svelte:head>

<div class="p-4 sm:p-6 lg:p-8 space-y-6 max-w-[1200px] mx-auto">
	<header class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
		<div>
			<h1 class="text-2xl font-bold text-dark-100">Setup</h1>
			<p class="text-sm text-dark-500 mt-1">What GreenKube detected, what is missing, and how to fix it</p>
		</div>
		<button type="button" class="btn-secondary text-sm" on:click={load} disabled={loading}>
			{loading ? 'Checking…' : 'Check again'}
		</button>
	</header>

	{#if loading && !status}
		<p class="text-sm text-dark-400" role="status">Checking the installation…</p>
	{:else if statusUnsupported}
		<div class="card text-sm text-dark-400" data-testid="setup-unsupported">
			This GreenKube server does not report its setup status yet. Upgrade GreenKube to get the guided checklist.
		</div>
	{:else if statusError}
		<div class="card text-sm text-amber-300" role="alert">{statusError}</div>
	{:else if status}
		<!-- Summary -->
		<section class="card-compact" aria-live="polite" data-testid="setup-summary">
			{#if status.complete}
				<p class="text-sm text-green-400">✓ Setup is complete: GreenKube can discover, deliver, track and measure changes.</p>
			{:else}
				<p class="text-sm text-yellow-200">
					Setup is not complete: {status.actionCount}
					{status.actionCount === 1 ? 'check needs' : 'checks need'} attention.
				</p>
			{/if}
		</section>

		{#if status.deliveryPaused}
			<p class="card-compact text-sm text-yellow-200" role="status" data-testid="delivery-paused">
				⏸ Delivery is paused by <code>automation.policy.pauseDelivery</code>: discovery, previews, pull-request
				tracking and measurement keep running, but no pull request is opened.
			</p>
		{/if}

		<!-- Capabilities -->
		<section aria-labelledby="capabilities-heading" class="space-y-2">
			<h2 id="capabilities-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Capabilities</h2>
			<ul class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
				{#each status.capabilities as cap (cap.id)}
					{@const display = capabilityDisplay(cap)}
					<li class="card-compact space-y-1" data-testid="capability-{cap.id}">
						<div class="flex items-center justify-between gap-2">
							<h3 class="text-sm font-medium text-dark-100">{cap.label}</h3>
							<StatusPill label={display.label} tone={display.tone} icon={display.icon} />
						</div>
						{#if cap.reason}<p class="text-xs text-dark-400" title={cap.reason}>{cap.reasonText}</p>{/if}
					</li>
				{/each}
			</ul>
		</section>

		<!-- Checklist -->
		<section aria-labelledby="checklist-heading" class="space-y-4">
			<h2 id="checklist-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Checklist</h2>
			{#each groups as group (group.id)}
				<div class="space-y-2" data-testid="check-group-{group.id}">
					<h3 class="text-xs font-semibold text-dark-400">{group.label}</h3>
					<ul class="space-y-2">
						{#each group.checks as check (check.id)}
							{@const info = CHECK_STATUS[check.status]}
							<li class="card-compact space-y-2" data-testid="check-{check.id}">
								<div class="flex flex-wrap items-start justify-between gap-2">
									<p class="text-sm font-medium text-dark-100">{check.title}</p>
									<StatusPill label={info.label} tone={info.tone} icon={info.icon} />
								</div>
								{#if check.reason}<p class="text-xs text-dark-400">{check.reason}</p>{/if}
								{#if check.status !== 'ok' && check.status !== 'not_applicable' && (check.fix.text || check.fix.snippet || check.fix.uiAction)}
									<div class="rounded-lg border border-dark-700/50 bg-dark-800/60 p-3 space-y-2">
										{#if check.fix.text}<p class="text-xs text-dark-200"><span class="font-semibold">Fix:</span> {check.fix.text}</p>{/if}
										{#if check.fix.snippet}
											<div class="relative">
												<pre class="overflow-x-auto rounded bg-dark-950 p-2 pr-16 text-[11px] text-dark-200"><code>{check.fix.snippet}</code></pre>
												<button
													type="button"
													class="absolute right-1 top-1 rounded bg-dark-800 px-2 py-0.5 text-[11px] text-dark-300 hover:text-white"
													aria-label="Copy the fix for {check.title}"
													on:click={() => copy(check.id, check.fix.snippet)}
												>
													{copied === check.id ? 'Copied' : 'Copy'}
												</button>
											</div>
										{/if}
										{#if check.fix.uiAction}
											{#if check.fix.uiAction.href}
												<a href={check.fix.uiAction.href} class="inline-block text-xs text-green-400 hover:text-green-300">{check.fix.uiAction.label} →</a>
											{:else}
												<p class="text-xs text-dark-300">{check.fix.uiAction.label}</p>
											{/if}
										{/if}
									</div>
								{/if}
								{#if check.docsUrl}
									<a href={check.docsUrl} target="_blank" rel="noopener noreferrer" class="text-xs text-dark-400 hover:text-dark-200">
										Documentation ↗
									</a>
								{/if}
							</li>
						{/each}
					</ul>
				</div>
			{:else}
				<p class="text-sm text-dark-400">The server reported no checks.</p>
			{/each}
		</section>
	{/if}

	<!-- Repository suggestions (F2) -->
	<section id="repository-suggestions" aria-labelledby="suggestions-heading" class="card space-y-3" data-testid="repository-suggestions">
		<h2 id="suggestions-heading" class="text-sm font-semibold uppercase tracking-wider text-dark-300">Repositories found by discovery</h2>
		{#if loading && !suggestions.length}
			<p class="text-sm text-dark-400" role="status">Loading…</p>
		{:else if suggestionsUnsupported}
			<p class="text-sm text-dark-400">This server does not suggest repositories yet; configure <code>automation.providers[].allowlist</code> in Helm values.</p>
		{:else if suggestionsError}
			<p class="text-sm text-amber-300" role="alert">{suggestionsError}</p>
		{:else if !suggestions.length}
			<p class="text-sm text-dark-400" data-testid="suggestions-empty">
				No repository discovered yet. Repositories appear once GreenKube discovers an Argo CD Application or Flux source.
			</p>
		{:else}
			{#if pending.length}
				<fieldset class="space-y-2">
					<legend class="text-xs text-dark-400 mb-1">Select the repositories GreenKube may open pull requests on:</legend>
					{#each pending as item (item.repoUrl)}
						<label class="flex items-start gap-3 rounded-lg border border-dark-700/50 p-3 hover:bg-dark-800/50">
							<input
								type="checkbox"
								class="mt-1"
								checked={selectedUrls.has(item.repoUrl)}
								on:change={() => toggle(item.repoUrl)}
								disabled={!isAdmin || allowing}
							/>
							<span class="min-w-0 text-xs">
								<span class="block text-sm text-dark-100 break-all">{item.repoUrl}</span>
								<span class="text-dark-400">
									{item.providerType}{#if item.apiBaseUrl} · {item.apiBaseUrl}{/if}
									{#if item.inferredFrom} · found in {item.inferredFrom}{/if}
								</span>
							</span>
						</label>
					{/each}
				</fieldset>
				{#if isAdmin}
					<button
						type="button"
						class="btn-primary text-sm disabled:opacity-50"
						on:click={allowSelected}
						disabled={!selectedCount || allowing}
						data-testid="allow-repositories"
					>
						{allowing ? 'Allowing…' : allowLabel(selectedCount)}
					</button>
				{:else}
					<p class="text-xs text-dark-400">An administrator must allow these repositories.</p>
				{/if}
			{:else}
				<p class="text-sm text-green-400">✓ Every discovered repository is allowlisted.</p>
			{/if}
			{#if allowError}<p class="text-xs text-amber-300" role="alert">{allowError}</p>{/if}
			{#if allowMessage}<p class="text-xs text-green-400" role="status">{allowMessage}</p>{/if}
			{#if allowSnippet}
				<div class="relative" data-testid="allowlist-snippet">
					<pre class="overflow-x-auto rounded bg-dark-950 p-2 pr-16 text-[11px] text-dark-200"><code>{allowSnippet}</code></pre>
					<button
						type="button"
						class="absolute right-1 top-1 rounded bg-dark-800 px-2 py-0.5 text-[11px] text-dark-300 hover:text-white"
						aria-label="Copy the allowlist values snippet"
						on:click={() => copy('allowlist-snippet', allowSnippet)}
					>
						{copied === 'allowlist-snippet' ? 'Copied' : 'Copy'}
					</button>
				</div>
			{/if}
			{#if allowlisted.length}
				<details class="text-xs">
					<summary class="cursor-pointer text-dark-400 hover:text-dark-200">Already allowlisted ({allowlisted.length})</summary>
					<ul class="mt-2 space-y-1">
						{#each allowlisted as item (item.repoUrl)}<li class="text-dark-300 break-all">✓ {item.repoUrl} <span class="text-dark-500">({item.providerType})</span></li>{/each}
					</ul>
				</details>
			{/if}
		{/if}
	</section>
</div>
