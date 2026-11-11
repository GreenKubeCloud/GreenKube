<script>
	/** Scoped API keys for machines: create (shown once), list, revoke (F15, K11). */
	import { onMount } from 'svelte';
	import { createApiKey, listApiKeys, revokeApiKey } from '$lib/api.js';
	import Card from '$lib/components/Card.svelte';
	import DataState from '$lib/components/DataState.svelte';
	import { formatDate } from '$lib/utils/format.js';

	const SCOPES = [
		{ value: 'read', label: 'read', help: 'Read every API resource.' },
		{ value: 'write', label: 'write', help: 'Scans, ignore/restore, mark applied, plan previews.' },
		{ value: 'approve', label: 'approve', help: 'Approve plans (opens pull requests).' },
		{ value: 'plans:deliver', label: 'plans:deliver', help: 'CI delivery: report the PR opened for an approved plan.' },
		{ value: 'signals:write', label: 'signals:write', help: 'Push recommendation signals.' },
		{ value: 'metrics:read', label: 'metrics:read', help: 'Prometheus scrape of /prometheus/metrics only.' },
		{ value: 'admin', label: 'admin', help: 'Everything, including API keys.' }
	];

	let keys = [];
	let loading = true;
	let error = null;

	let name = '';
	let selectedScopes = ['read'];
	let expiresInDays = '';
	let creating = false;
	let createError = '';
	/** The full key of the last created key; shown once, never stored. */
	let created = null;
	let copied = false;
	let actionError = '';

	async function load() {
		loading = true;
		error = null;
		try {
			keys = await listApiKeys();
		} catch (e) {
			error = e.status === 403 ? 'Only administrators can manage API keys.' : e.message;
		} finally {
			loading = false;
		}
	}

	async function create() {
		createError = '';
		copied = false;
		if (!name.trim()) {
			createError = 'Give the key a name.';
			return;
		}
		if (selectedScopes.length === 0) {
			createError = 'Select at least one scope.';
			return;
		}
		creating = true;
		try {
			created = await createApiKey({ name: name.trim(), scopes: selectedScopes, expiresInDays: expiresInDays || null });
			name = '';
			expiresInDays = '';
			selectedScopes = ['read'];
			await load();
		} catch (e) {
			createError = e.message;
		} finally {
			creating = false;
		}
	}

	async function copyKey() {
		try {
			await navigator.clipboard.writeText(created.key);
			copied = true;
		} catch {
			copied = false;
		}
	}

	async function revoke(key) {
		actionError = '';
		if (!window.confirm(`Revoke the API key "${key.name}" (${key.prefix})? Clients using it stop working.`)) return;
		try {
			await revokeApiKey(key.id);
			await load();
		} catch (e) {
			actionError = e.message;
		}
	}

	onMount(load);
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1400px] mx-auto">
	<div>
		<h1 class="text-2xl font-bold text-dark-100">API keys</h1>
		<p class="text-sm text-dark-500 mt-1">
			Keys for machines: CI pipelines, Prometheus, scripts. Send them as
			<code>Authorization: Bearer gk_…</code>. People sign in with their account instead.
		</p>
	</div>

	{#if created}
		<div class="rounded-xl border border-green-700/50 bg-green-900/20 p-4 space-y-2" role="status" data-testid="new-api-key">
			<p class="text-sm font-medium text-green-300">
				Key "{created.name}" created. Copy it now: it is shown only once.
			</p>
			<div class="flex items-center gap-2">
				<code class="flex-1 break-all rounded bg-dark-950 px-2 py-1 text-xs text-dark-100">{created.key}</code>
				<button type="button" class="btn-secondary text-xs !py-1 !px-2" on:click={copyKey}>
					{copied ? 'Copied' : 'Copy'}
				</button>
			</div>
			<button type="button" class="text-xs text-dark-400 hover:text-dark-200" on:click={() => (created = null)}>
				I have stored the key
			</button>
		</div>
	{/if}

	<Card title="Create a key" icon="➕">
		<form class="space-y-4" aria-label="Create an API key" on:submit|preventDefault={create}>
			<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
				<div>
					<label for="key-name" class="block text-xs font-medium text-dark-400 mb-1">Name</label>
					<input
						id="key-name"
						type="text"
						bind:value={name}
						maxlength="100"
						placeholder="prometheus, ci-pipeline…"
						autocomplete="off"
						class="w-full px-3 py-2 bg-dark-800 border border-dark-600/50 rounded-lg text-sm text-dark-200"
					/>
				</div>
				<div>
					<label for="key-expiry" class="block text-xs font-medium text-dark-400 mb-1">Expires after (days, optional)</label>
					<input
						id="key-expiry"
						type="number"
						min="1"
						max="3650"
						bind:value={expiresInDays}
						placeholder="Never"
						class="w-full px-3 py-2 bg-dark-800 border border-dark-600/50 rounded-lg text-sm text-dark-200"
					/>
				</div>
			</div>
			<fieldset>
				<legend class="text-xs font-medium text-dark-400 mb-2">Scopes</legend>
				<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
					{#each SCOPES as scope (scope.value)}
						<label class="flex items-start gap-2 rounded-lg bg-dark-800/50 p-2 text-sm text-dark-300">
							<input type="checkbox" bind:group={selectedScopes} value={scope.value} class="mt-1" />
							<span>
								<span class="font-mono text-dark-200">{scope.label}</span>
								<span class="block text-[11px] text-dark-500">{scope.help}</span>
							</span>
						</label>
					{/each}
				</div>
			</fieldset>
			{#if createError}
				<p class="text-xs text-red-400" role="alert">{createError}</p>
			{/if}
			<button type="submit" class="btn-primary text-sm" disabled={creating}>
				{creating ? 'Creating…' : 'Create key'}
			</button>
		</form>
	</Card>

	<Card title="Keys" icon="🔑">
		<DataState {loading} {error} empty={keys.length === 0} emptyMessage="No API key yet.">
			<div class="overflow-x-auto -mx-5 px-5">
				<table class="w-full text-sm" aria-label="API keys">
					<thead>
						<tr class="border-b border-dark-700/50 text-left text-xs uppercase tracking-wider text-dark-500">
							<th class="py-2 px-2">Name</th>
							<th class="py-2 px-2">Key</th>
							<th class="py-2 px-2">Scopes</th>
							<th class="py-2 px-2">Created</th>
							<th class="py-2 px-2">Last used</th>
							<th class="py-2 px-2">Status</th>
							<th class="py-2 px-2"><span class="sr-only">Actions</span></th>
						</tr>
					</thead>
					<tbody class="divide-y divide-dark-800">
						{#each keys as key (key.id)}
							<tr>
								<td class="py-2 px-2 text-dark-200">{key.name}</td>
								<td class="py-2 px-2 font-mono text-xs text-dark-400">{key.prefix}_…</td>
								<td class="py-2 px-2 font-mono text-xs text-dark-300">{key.scopes.join(', ')}</td>
								<td class="py-2 px-2 text-xs text-dark-400">
									{formatDate(key.created_at)}
									{#if key.created_by}<span class="block text-dark-600">{key.created_by}</span>{/if}
								</td>
								<td class="py-2 px-2 text-xs text-dark-400">{key.last_used_at ? formatDate(key.last_used_at) : 'Never'}</td>
								<td class="py-2 px-2 text-xs">
									{#if key.active}
										<span class="badge-green">Active</span>
										{#if key.expires_at}<span class="block text-dark-500">until {formatDate(key.expires_at)}</span>{/if}
									{:else if key.revoked_at}
										<span class="text-dark-500">Revoked</span>
									{:else}
										<span class="text-dark-500">Expired</span>
									{/if}
								</td>
								<td class="py-2 px-2 text-right">
									{#if key.active}
										<button
											type="button"
											class="btn-secondary text-xs !py-1 !px-2"
											on:click={() => revoke(key)}
											aria-label={`Revoke ${key.name}`}
										>
											Revoke
										</button>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
			{#if actionError}
				<p class="mt-2 text-xs text-red-400" role="alert">{actionError}</p>
			{/if}
		</DataState>
	</Card>
</div>
