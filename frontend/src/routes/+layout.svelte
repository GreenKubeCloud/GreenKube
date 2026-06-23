<script>
	import '../app.css';
	import { page } from '$app/stores';
	import { sidebarCollapsed, servicesHealth, healthPopupDismissed } from '$lib/stores.js';
	import { getHealth, getServicesHealth } from '$lib/api.js';
	import { onMount } from 'svelte';
	import HealthBadge from '$lib/components/HealthBadge.svelte';
	import HealthPopup from '$lib/components/HealthPopup.svelte';

	let health = null;
	let healthError = false;
	let showHealthPopup = false;

	const navItems = [
		{ href: '/', label: 'Dashboard', icon: 'dashboard' },
		{ href: '/recommendations', label: 'Recommendations', icon: 'recommendations' },
		{ href: '/report', label: 'Report', icon: 'report' },
		{ href: '/nodes', label: 'Nodes', icon: 'nodes' },
		{ href: '/settings', label: 'Settings', icon: 'settings' }
	];

	const navIcons = {
		dashboard: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 5a1 1 0 011-1h4a1 1 0 011 1v5a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v2a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zm0 6a1 1 0 011-1h4a1 1 0 011 1v5a1 1 0 01-1 1h-4a1 1 0 01-1-1v-5zM4 13a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4z"/>',
		recommendations: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>',
		report: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>',
		nodes: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01"/>',
		settings: '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>'
	};

	onMount(async () => {
		try {
			health = await getHealth();
		} catch {
			healthError = true;
		}

		// Fetch services health on first load
		try {
			const result = await getServicesHealth();
			servicesHealth.set(result);

			// Show popup if there are connectivity issues
			const services = result?.services || {};
			const hasIssues = Object.values(services).some(
				s => s.status === 'unreachable' || s.status === 'unconfigured'
			);
			if (hasIssues && !$healthPopupDismissed) {
				showHealthPopup = true;
			}
		} catch (e) {
			// Silently fail — health check is non-blocking
		}
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
	$: worstStatus = (() => {
		const statuses = Object.values(svcData).map(s => s.status);
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
					<svg class="w-4.5 h-4.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						{@html navIcons[item.icon]}
					</svg>
					{#if !$sidebarCollapsed}
						<span class="truncate">{item.label}</span>
					{/if}
				</a>
			{/each}
		</nav>

		<!-- Bottom section -->
		<div class="p-3 border-t border-dark-700/50">
			<!-- Service health indicators -->
			{#if Object.keys(svcData).length > 0}
				<div class="space-y-1 mb-2">
					{#each Object.entries(svcData) as [name, svc]}
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
