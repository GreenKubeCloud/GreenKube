<script>
	import '../app.css';
	import { page } from '$app/stores';
	import { sidebarCollapsed, servicesHealth, healthPopupDismissed, currentUser, hasScope } from '$lib/stores.js';
	import {
		ApiError,
		AUTH_REQUIRED_EVENT,
		PASSWORD_CHANGE_REQUIRED_EVENT,
		getHealth,
		getMe,
		getServicesHealth,
		logout
	} from '$lib/api.js';
	import { onMount } from 'svelte';
	import HealthPopup from '$lib/components/HealthPopup.svelte';
	import LoginForm from '$lib/components/LoginForm.svelte';
	import PasswordChangeForm from '$lib/components/PasswordChangeForm.svelte';

	let health = null;
	let healthError = false;
	let showHealthPopup = false;
	/** loading | signed-out | password-change | ready | error */
	let authState = 'loading';
	let authError = '';
	let initialSecretErased = false;
	let insecureRemote = false;
	// Bumped after a sign-in so the current page remounts and reloads its data.
	let authEpoch = 0;

	const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);

	const navItems = [
		{ href: '/', label: 'Dashboard' },
		{ href: '/recommendations', label: 'Recommendations' },
		{ href: '/automation', label: 'Automation' },
		{ href: '/report', label: 'Report' },
		{ href: '/nodes', label: 'Nodes' },
		{ href: '/settings', label: 'Settings' }
	];

	async function refreshServicesHealth() {
		try {
			const result = await getServicesHealth();
			servicesHealth.set(result);

			const services = result?.services || {};
			const hasIssues = Object.values(services).some(
				s => (s.status === 'unreachable' || s.status === 'unconfigured') && !s.inactive
			);
			if (hasIssues && !$healthPopupDismissed) {
				showHealthPopup = true;
			}
		} catch {
			// The sidebar indicators stay empty; a 401 already switched to the sign-in view.
		}
	}

	function enter(principal) {
		currentUser.set(principal);
		authError = '';
		if (principal?.must_change_password) {
			authState = 'password-change';
			return;
		}
		authState = 'ready';
		authEpoch += 1;
		refreshServicesHealth();
	}

	async function loadPrincipal() {
		authState = 'loading';
		try {
			enter(await getMe());
		} catch (error) {
			currentUser.set(null);
			if (error instanceof ApiError && error.status === 401) {
				authState = 'signed-out';
			} else {
				authState = 'error';
				authError = error?.message || 'GreenKube is not reachable.';
			}
		}
	}

	function handleSignedIn(event) {
		enter(event.detail);
	}

	function handlePasswordChanged(event) {
		initialSecretErased = Boolean(event.detail?.initial_admin_secret_erased);
		enter(event.detail);
	}

	function handleAuthRequired() {
		currentUser.set(null);
		showHealthPopup = false;
		authState = 'signed-out';
	}

	function handlePasswordChangeRequired() {
		authState = 'password-change';
	}

	async function signOut() {
		try {
			await logout();
		} catch {
			// The server session may already be gone; forget it locally anyway.
		}
		currentUser.set(null);
		showHealthPopup = false;
		authState = 'signed-out';
	}

	onMount(() => {
		insecureRemote = window.location.protocol === 'http:' && !LOCAL_HOSTS.has(window.location.hostname);
		window.addEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
		window.addEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, handlePasswordChangeRequired);
		(async () => {
			try {
				health = await getHealth();
			} catch {
				healthError = true;
			}
		})();
		loadPrincipal();

		return () => {
			window.removeEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
			window.removeEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, handlePasswordChangeRequired);
		};
	});

	function handlePopupDismiss() {
		showHealthPopup = false;
		healthPopupDismissed.set(true);
	}

	function isActive(href, pathname) {
		if (href === '/') return pathname === '/';
		return pathname.startsWith(href);
	}

	// Compute sidebar health indicators from services health
	$: svcData = $servicesHealth?.services || {};
	$: activeServices = Object.fromEntries(Object.entries(svcData).filter(([, s]) => !s.inactive));
	$: worstStatus = (() => {
		const statuses = Object.values(activeServices).map(s => s.status);
		if (statuses.includes('unreachable')) return 'unreachable';
		if (statuses.includes('unconfigured')) return 'unconfigured';
		if (statuses.includes('degraded')) return 'degraded';
		if (statuses.length > 0) return 'healthy';
		return null;
	})();

	const statusColors = {
		healthy: 'bg-green-500',
		degraded: 'bg-yellow-500',
		unreachable: 'bg-red-500',
		unconfigured: 'bg-dark-500'
	};
</script>

{#if authState === 'loading'}
	<div class="flex h-screen items-center justify-center bg-dark-950 text-sm text-dark-500" role="status">
		Loading…
	</div>
{:else if authState === 'signed-out' || authState === 'password-change' || authState === 'error'}
	<div class="flex min-h-screen flex-col bg-dark-950">
		{#if insecureRemote}
			<div
				class="border-b border-yellow-700/50 bg-yellow-900/40 px-4 py-2 text-center text-xs text-yellow-200"
				role="status"
				data-testid="tls-warning"
			>
				This connection is not encrypted (plain HTTP). Your password and session can be read on the
				network: serve GreenKube over HTTPS (Ingress with TLS) for remote access.
			</div>
		{/if}
		<main class="flex flex-1 items-center justify-center p-4">
			<div class="w-full max-w-md rounded-2xl border border-dark-700/50 bg-dark-900 p-6 shadow-2xl">
				<div class="mb-5 flex items-center gap-3">
					<img src="/greenkube-logo.png" alt="" class="h-8 w-8 rounded-lg" />
					<h1 class="text-lg font-bold text-dark-100">
						{authState === 'password-change' ? 'Choose a new password' : 'Sign in to GreenKube'}
					</h1>
				</div>
				{#if authState === 'signed-out'}
					<LoginForm on:signedin={handleSignedIn} />
					<p class="mt-4 text-xs text-dark-500">
						First sign-in? Use the initial admin password printed by <code>helm install</code>.
						Scripts and Prometheus use API keys instead.
					</p>
				{:else if authState === 'password-change'}
					<p class="mb-4 text-sm text-dark-400">
						You signed in with the initial password. Choose your own password to continue; the
						initial password then stops working and is erased from its Kubernetes Secret.
					</p>
					<PasswordChangeForm
						username={$currentUser?.name || ''}
						submitLabel="Save and continue"
						on:changed={handlePasswordChanged}
					/>
					<button type="button" class="mt-3 text-xs text-dark-500 hover:text-dark-300" on:click={signOut}>
						Sign out
					</button>
				{:else}
					<p class="text-sm text-red-400" role="alert">{authError}</p>
					<button type="button" class="btn-secondary mt-4 text-sm" on:click={loadPrincipal}>Retry</button>
				{/if}
			</div>
		</main>
	</div>
{:else}
<!-- Health Popup (shown once on first load if issues detected) -->
<HealthPopup
	services={$servicesHealth?.services}
	visible={showHealthPopup}
	on:dismiss={handlePopupDismiss}
/>

<div class="flex h-screen overflow-hidden">
	<!-- Sidebar -->
	<aside class="flex flex-col border-r border-dark-700/50 bg-dark-900 transition-all duration-300
	              {$sidebarCollapsed ? 'w-16' : 'w-60'}">
		<!-- Logo -->
		<div class="flex items-center gap-3 px-4 py-5 border-b border-dark-700/50">
			<img src="/greenkube-logo.png" alt="GreenKube" class="w-8 h-8 rounded-lg shrink-0" />
			{#if !$sidebarCollapsed}
				<div class="overflow-hidden">
					<h1 class="text-base font-bold text-dark-100 truncate">GreenKube</h1>
					<p class="text-[10px] text-dark-500 truncate">FinGreenOps Platform</p>
				</div>
			{/if}
		</div>

		<!-- Navigation -->
		<nav class="flex-1 py-4 px-2 space-y-1 overflow-y-auto">
			{#each navItems as item}
				<a
					href={item.href}
					class="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all duration-200
					       {isActive(item.href, $page.url.pathname)
								? 'bg-green-600/15 text-green-400 font-medium'
								: 'text-dark-400 hover:text-dark-200 hover:bg-dark-800'}"
				>
					<!-- no icons -->
					{#if !$sidebarCollapsed}
						<span class="truncate">{item.label}</span>
					{/if}
				</a>
			{/each}
		</nav>

		<!-- Bottom section -->
		<div class="p-3 border-t border-dark-700/50">
			<!-- Service health indicators -->
			{#if Object.keys(activeServices).length > 0}
				<div class="space-y-1 mb-2">
					{#each Object.entries(activeServices) as [name, svc]}
						<div class="flex items-center gap-2 px-2 py-0.5" title="{svc.message}">
							<div class="w-1.5 h-1.5 rounded-full shrink-0 {statusColors[svc.status] || 'bg-dark-500'}
							            {svc.status === 'degraded' ? 'animate-pulse' : ''}"></div>
							{#if !$sidebarCollapsed}
								<span class="text-[10px] text-dark-500 truncate capitalize">{name.replace('_', ' ')}</span>
							{/if}
						</div>
					{/each}
				</div>
			{/if}

			<!-- API health indicator -->
			<div class="flex items-center gap-2 px-2 py-1.5">
				<div class="w-2 h-2 rounded-full shrink-0
				            {healthError ? 'bg-red-500' : health ? 'bg-green-500' : 'bg-yellow-500 animate-pulse'}">
				</div>
				{#if !$sidebarCollapsed}
					<span class="text-xs text-dark-500 truncate">
						{healthError ? 'API offline' : health ? (health.version.startsWith('v') ? health.version : `v${health.version}`) : 'Connecting…'}
					</span>
				{/if}
			</div>

			{#if $currentUser}
				<div class="mt-1 border-t border-dark-700/50 pt-2" data-testid="account-menu">
					{#if $currentUser.kind === 'anonymous'}
						{#if !$sidebarCollapsed}
							<p class="px-2 text-[10px] text-yellow-500" title="No account exists yet and no API key is configured.">
								Sign-in disabled (development)
							</p>
						{/if}
					{:else}
						{#if !$sidebarCollapsed}
							<p class="truncate px-2 text-xs text-dark-300" title={$currentUser.actor}>
								{$currentUser.name}
								{#if $currentUser.role}<span class="text-dark-500">· {$currentUser.role}</span>{/if}
							</p>
						{/if}
						{#if $currentUser.kind === 'user'}
							<a
								href="/settings/account"
								class="block rounded-lg px-2 py-1 text-xs text-dark-500 hover:bg-dark-800 hover:text-dark-200"
							>
								{$sidebarCollapsed ? 'Acct' : 'Account & sessions'}
							</a>
						{/if}
						{#if hasScope($currentUser, 'admin')}
							<a
								href="/settings/api-keys"
								class="block rounded-lg px-2 py-1 text-xs text-dark-500 hover:bg-dark-800 hover:text-dark-200"
							>
								{$sidebarCollapsed ? 'Keys' : 'API keys'}
							</a>
						{/if}
						{#if $currentUser.kind === 'user'}
							<button
								type="button"
								on:click={signOut}
								class="flex w-full items-center gap-2 rounded-lg px-2 py-1 text-xs text-dark-500 transition-colors hover:bg-dark-800 hover:text-dark-200"
								aria-label="Sign out"
							>
								<svg class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
										  d="M15 12H3m0 0l4-4m-4 4l4 4m6-10h5a2 2 0 012 2v12a2 2 0 01-2 2h-5" />
								</svg>
								{#if !$sidebarCollapsed}<span>Sign out</span>{/if}
							</button>
						{/if}
					{/if}
				</div>
			{/if}

			<!-- Collapse toggle -->
			<button
				on:click={() => sidebarCollapsed.update(v => !v)}
				class="w-full flex items-center justify-center mt-1 py-1.5 rounded-lg
				       text-dark-500 hover:text-dark-300 hover:bg-dark-800 transition-colors"
				aria-label="Toggle sidebar"
			>
				<svg class="w-4 h-4 transition-transform {$sidebarCollapsed ? 'rotate-180' : ''}"
					 fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
						  d="M11 19l-7-7 7-7m8 14l-7-7 7-7" />
				</svg>
			</button>
		</div>
	</aside>

	<!-- Main Content -->
	<main class="flex-1 overflow-y-auto">
		{#if insecureRemote}
			<div
				class="border-b border-yellow-700/50 bg-yellow-900/40 px-4 py-2 text-center text-xs text-yellow-200"
				role="status"
				data-testid="tls-warning"
			>
				This connection is not encrypted (plain HTTP). Your password and session can be read on the
				network: serve GreenKube over HTTPS (Ingress with TLS) for remote access.
			</div>
		{/if}
		{#if initialSecretErased}
			<div class="border-b border-green-700/40 bg-green-900/30 px-4 py-2 text-center text-xs text-green-300" role="status">
				Password changed. The initial admin password was erased from its Kubernetes Secret.
			</div>
		{/if}
		{#key authEpoch}
			<slot />
		{/key}
	</main>
</div>
{/if}
