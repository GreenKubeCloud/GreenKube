<script>
	/** Account: role, password change, active sessions with revoke, sign out (K11); users and roles for admins (F16). */
	import { onMount } from 'svelte';
	import { listSessions, revokeSession, logout } from '$lib/api.js';
	import { currentUser, hasScope } from '$lib/stores.js';
	import UsersCard from './UsersCard.svelte';
	import Card from '$lib/components/Card.svelte';
	import DataState from '$lib/components/DataState.svelte';
	import PasswordChangeForm from '$lib/components/PasswordChangeForm.svelte';
	import { formatDate } from '$lib/utils/format.js';

	let sessions = [];
	let loading = true;
	let error = null;
	let message = '';
	let actionError = '';

	async function load() {
		loading = true;
		error = null;
		try {
			sessions = await listSessions();
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	async function revoke(session) {
		actionError = '';
		try {
			await revokeSession(session.id);
			if (session.current) {
				window.location.assign('/');
				return;
			}
			sessions = sessions.filter((item) => item.id !== session.id);
		} catch (e) {
			actionError = e.message;
		}
	}

	async function signOut() {
		try {
			await logout();
		} finally {
			currentUser.set(null);
			window.location.assign('/');
		}
	}

	function handleChanged(event) {
		currentUser.set(event.detail);
		message = event.detail?.initial_admin_secret_erased
			? 'Password changed. Your other sessions were signed out and the initial password was erased.'
			: 'Password changed. Your other sessions were signed out.';
		load();
	}

	function browserLabel(userAgent) {
		if (!userAgent) return 'Unknown client';
		const match = userAgent.match(/(Firefox|Edg|Chrome|Safari)\/[\d.]+/);
		const os = userAgent.match(/\(([^;)]+)/)?.[1];
		return [match?.[1] === 'Edg' ? 'Edge' : match?.[1], os].filter(Boolean).join(' · ') || userAgent.slice(0, 60);
	}

	const ROLE_HELP = {
		viewer: 'read everything',
		operator: 'approve plans, ignore and restore recommendations, mark them applied',
		admin: 'everything, including users, API keys and the repository allowlist'
	};
	const SOURCE_LABEL = { local: 'local account', proxy: 'single sign-on (authentication proxy)', oidc: 'single sign-on (OpenID Connect)' };

	$: isSso = $currentUser?.auth_source === 'proxy' || $currentUser?.auth_source === 'oidc';
	// Signed in with an API key (API_AUTH_MODE=api_key, F-09): no password and no sessions.
	$: isApiKey = $currentUser?.kind === 'apikey';

	onMount(() => {
		if ($currentUser?.kind === 'apikey') {
			loading = false;
			return;
		}
		load();
	});
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1200px] mx-auto">
	<div>
		<h1 class="text-2xl font-bold text-dark-100">Account</h1>
		<p class="text-sm text-dark-500 mt-1">
			Signed in as <span class="text-dark-300">{$currentUser?.name ?? '—'}</span>
			{#if $currentUser?.auth_source}
				<span class="text-dark-500">· {SOURCE_LABEL[$currentUser.auth_source] ?? $currentUser.auth_source}</span>
			{/if}
		</p>
		{#if $currentUser?.role}
			<p class="text-sm text-dark-400 mt-1" data-testid="account-role">
				Role <span class="badge-green">{$currentUser.role}</span>
				<span class="text-dark-500">— {ROLE_HELP[$currentUser.role] ?? ''}</span>
				{#if isSso}<span class="text-dark-500">(from your identity provider groups)</span>{/if}
			</p>
		{/if}
	</div>

	{#if isApiKey}
		<Card title="API key" icon="🔑">
			<div slot="actions">
				<button type="button" class="btn-secondary text-xs !py-1.5 !px-3" on:click={signOut}>Sign out</button>
			</div>
			<p class="text-sm text-dark-400" data-testid="api-key-account">
				This browser is signed in with an API key (<code>apiAuthMode: api_key</code>): there is no password or
				session to manage. Revoking the key signs this browser out.
			</p>
		</Card>
	{:else}
		<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
			<Card title={isSso ? 'Password' : 'Change password'} icon="🔑">
				{#if isSso}
					<p class="text-sm text-dark-400" data-testid="sso-password">
						You sign in through single sign-on: change your password at your identity provider.
					</p>
				{:else}
					{#if message}
						<p class="mb-3 text-xs text-green-400" role="status">{message}</p>
					{/if}
					<PasswordChangeForm username={$currentUser?.name || ''} on:changed={handleChanged} />
				{/if}
			</Card>

			<Card title="Active sessions" icon="🖥️">
				<div slot="actions">
					<button type="button" class="btn-secondary text-xs !py-1.5 !px-3" on:click={signOut}>Sign out</button>
				</div>
				<DataState {loading} {error} empty={sessions.length === 0}>
					<ul class="divide-y divide-dark-800" aria-label="Active sessions">
						{#each sessions as session (session.id)}
							<li class="flex items-center justify-between gap-3 py-3">
								<div class="min-w-0">
									<p class="text-sm text-dark-200 truncate">
										{browserLabel(session.user_agent)}
										{#if session.current}<span class="badge-green ml-2">This browser</span>{/if}
										{#if session.remember}<span class="ml-2 text-[10px] text-dark-500">kept signed in</span>{/if}
									</p>
									<p class="text-xs text-dark-500">
										{session.ip ?? 'unknown address'} · signed in {formatDate(session.created_at)} · last
										active {formatDate(session.last_seen_at)}
									</p>
								</div>
								<button
									type="button"
									class="btn-secondary text-xs !py-1 !px-2 shrink-0"
									on:click={() => revoke(session)}
									aria-label={session.current ? 'Sign out this browser' : `Revoke session ${session.id}`}
								>
									{session.current ? 'Sign out' : 'Revoke'}
								</button>
							</li>
						{/each}
					</ul>
					{#if actionError}
						<p class="mt-2 text-xs text-red-400" role="alert">{actionError}</p>
					{/if}
				</DataState>
			</Card>
		</div>
	{/if}

	{#if hasScope($currentUser, 'admin') && $currentUser?.kind === 'user'}
		<UsersCard currentUserName={$currentUser?.name || ''} />
	{/if}
</div>
