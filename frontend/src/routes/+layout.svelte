<script>
	import '../app.css';
	import { page } from '$app/stores';
	import { sidebarCollapsed, servicesHealth, healthPopupDismissed } from '$lib/stores.js';
	import { ApiError, clearApiToken, getHealth, getServicesHealth, setApiToken } from '$lib/api.js';
	import { onMount } from 'svelte';
	import HealthBadge from '$lib/components/HealthBadge.svelte';
	import HealthPopup from '$lib/components/HealthPopup.svelte';

	let health = null;
	let healthError = false;
	let showHealthPopup = false;
	let showAuthDialog = false;
	let authToken = '';
	let authError = '';
	let authenticating = false;

	const navItems = [
		{ href: '/', label: 'Dashboard' },
		{ href: '/recommendations', label: 'Recommendations' },
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
			return true;
		} catch (error) {
			if (error instanceof ApiError && error.status === 401) {
				showAuthDialog = true;
			}
			return false;
		}
	}

	async function authenticate() {
		const token = authToken.trim();
		if (!token) {
			authError = 'Enter an API key.';
			return;
		}
		authenticating = true;
		authError = '';
		setApiToken(token);
		if (await refreshServicesHealth()) {
			showAuthDialog = false;
			authToken = '';
		} else {
			clearApiToken();
			authError = 'The API key was rejected.';
		}
		authenticating = false;
	}

	function handleAuthRequired() {
		showAuthDialog = true;
		authError = '';
	}

	onMount(async () => {
		window.addEventListener('greenkube-auth-required', handleAuthRequired);
		try {
			health = await getHealth();
		} catch {
			healthError = true;
		}

		await refreshServicesHealth();

		return () => window.removeEventListener('greenkube-auth-required', handleAuthRequired);
	});

	function handlePopupDismiss() {
		showHealthPopup = false;
		healthPopupDismissed.set(true);
	}

	function handlePopupUpdated(event) {
		servicesHealth.set(event.detail);
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

{#if showAuthDialog}
	<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70" role="presentation">
		<div
			class="w-full max-w-md rounded-2xl border border-dark-700/50 bg-dark-900 p-6 shadow-2xl"
			role="dialog"
			aria-modal="true"
			aria-labelledby="api-auth-title"
		>
			<h2 id="api-auth-title" class="text-lg font-bold text-dark-100">API authentication required</h2>
			<p class="mt-2 text-sm text-dark-400">
				This GreenKube instance requires an API key. The key is kept in memory only.
			</p>
			<form class="mt-5 space-y-4" on:submit|preventDefault={authenticate}>
				<label for="api-token" class="block text-sm font-medium text-dark-300">API key</label>
				<input
					id="api-token"
					type="password"
					bind:value={authToken}
					autocomplete="off"
					class="w-full rounded-lg border border-dark-700 bg-dark-800 px-3 py-2 text-dark-100 outline-none focus:border-green-500"
					placeholder="Paste the GreenKube API key"
				/>
				{#if authError}
					<p class="text-sm text-red-400" role="alert">{authError}</p>
				{/if}
				<button
					type="submit"
					disabled={authenticating}
					class="w-full rounded-lg bg-green-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-green-500 disabled:cursor-wait disabled:opacity-60"
				>
					{authenticating ? 'Connecting…' : 'Connect'}
				</button>
			</form>
		</div>
	</div>
{/if}

<!-- Health Popup (shown once on first load if issues detected) -->
<HealthPopup
	services={$servicesHealth?.services}
	visible={showHealthPopup}
	on:dismiss={handlePopupDismiss}
	on:updated={handlePopupUpdated}
/>

<div class="flex h-screen overflow-hidden">
	<!-- Sidebar -->
	<aside class="flex flex-col border-r border-dark-700/50 bg-dark-900 transition-all duration-300
	              {$sidebarCollapsed ? 'w-16' : 'w-60'}">
		<!-- Logo -->
		<div class="flex items-center gap-3 px-4 py-5 border-b border-dark-700/50">
			<img src="/greenkube-logo.png" alt="GreenKube" class="w-8 h-8 rounded-lg flex-shrink-0" />
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
							<div class="w-1.5 h-1.5 rounded-full flex-shrink-0 {statusColors[svc.status] || 'bg-dark-500'}
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
				<div class="w-2 h-2 rounded-full flex-shrink-0
				            {healthError ? 'bg-red-500' : health ? 'bg-green-500' : 'bg-yellow-500 animate-pulse'}">
				</div>
				{#if !$sidebarCollapsed}
					<span class="text-xs text-dark-500 truncate">
						{healthError ? 'API offline' : health ? (health.version.startsWith('v') ? health.version : `v${health.version}`) : 'Connecting…'}
					</span>
				{/if}
			</div>

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
		<slot />
	</main>
</div>
