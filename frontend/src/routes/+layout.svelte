<script>
	import '../app.css';
	import { page } from '$app/stores';
	import { sidebarCollapsed } from '$lib/stores.js';
	import { getHealth } from '$lib/api.js';
	import { onMount } from 'svelte';

	let health = null;
	let healthError = false;

	const navItems = [
		{ href: '/', label: 'Dashboard', icon: '📊' },
		{ href: '/metrics', label: 'Metrics', icon: '📈' },
		{ href: '/nodes', label: 'Nodes', icon: '🖥️' },
		{ href: '/recommendations', label: 'Recommendations', icon: '💡' },
		{ href: '/settings', label: 'Settings', icon: '⚙️' }
	];

	onMount(async () => {
		try {
			health = await getHealth();
		} catch {
			healthError = true;
		}
	});

	function isActive(href, pathname) {
		if (href === '/') return pathname === '/';
		return pathname.startsWith(href);
	}
</script>

<div class="flex h-screen overflow-hidden">
	<!-- Sidebar -->
	<aside class="flex flex-col border-r border-dark-700/50 bg-dark-900 transition-all duration-300
	              {$sidebarCollapsed ? 'w-16' : 'w-60'}">
		<!-- Logo -->
		<div class="flex items-center gap-3 px-4 py-5 border-b border-dark-700/50">
			<div class="w-8 h-8 bg-green-600 rounded-lg flex items-center justify-center flex-shrink-0">
				<span class="text-white font-bold text-sm">G</span>
			</div>
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
					<span class="text-base flex-shrink-0">{item.icon}</span>
					{#if !$sidebarCollapsed}
						<span class="truncate">{item.label}</span>
					{/if}
				</a>
			{/each}
		</nav>

		<!-- Bottom section -->
		<div class="p-3 border-t border-dark-700/50">
			<!-- Health indicator -->
			<div class="flex items-center gap-2 px-2 py-1.5">
				<div class="w-2 h-2 rounded-full flex-shrink-0
				            {healthError ? 'bg-red-500' : health ? 'bg-green-500' : 'bg-yellow-500 animate-pulse'}">
				</div>
				{#if !$sidebarCollapsed}
					<span class="text-xs text-dark-500 truncate">
						{healthError ? 'API offline' : health ? `v${health.version}` : 'Connecting…'}
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
