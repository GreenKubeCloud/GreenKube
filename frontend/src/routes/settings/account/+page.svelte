<script>
	/** Account: password change, active sessions with revoke, sign out (K11). */
	import { onMount } from 'svelte';
	import { listSessions, revokeSession, logout } from '$lib/api.js';
	import { currentUser } from '$lib/stores.js';
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

	onMount(load);
</script>

<div class="p-6 lg:p-8 space-y-6 max-w-[1200px] mx-auto">
	<div>
		<h1 class="text-2xl font-bold text-dark-100">Account</h1>
		<p class="text-sm text-dark-500 mt-1">
			Signed in as <span class="text-dark-300">{$currentUser?.name ?? '—'}</span>
			{#if $currentUser?.role}({$currentUser.role}){/if}
		</p>
	</div>

	<div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
		<Card title="Change password" icon="🔑">
			{#if message}
				<p class="mb-3 text-xs text-green-400" role="status">{message}</p>
			{/if}
			<PasswordChangeForm username={$currentUser?.name || ''} on:changed={handleChanged} />
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
</div>
