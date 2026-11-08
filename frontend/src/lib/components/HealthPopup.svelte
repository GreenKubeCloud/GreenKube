<script>
	/**
	 * Connectivity issues found at load. Data sources are configuration as code
	 * (A13): the popup explains which Helm values fix each issue instead of
	 * editing them at runtime.
	 */
	import { createEventDispatcher } from 'svelte';
	import HealthBadge from './HealthBadge.svelte';
	import { helmSnippetFor } from '$lib/configSnippets.js';

	/** @type {Object|null} */
	export let services = null;
	/** @type {boolean} */
	export let visible = false;

	const dispatch = createEventDispatcher();

	$: issues = services ? Object.values(services).filter(
		s => (s.status === 'unreachable' || s.status === 'unconfigured') && !s.inactive
	) : [];

	$: hasIssues = issues.length > 0;
	$: fixes = issues
		.map((svc) => ({ svc, snippet: helmSnippetFor(svc.name) }))
		.filter((item) => item.snippet);

	function dismiss() {
		dispatch('dismiss');
	}

	function handleEscape(event) {
		if (event.key === 'Escape' && visible && hasIssues) dismiss();
	}
</script>

<svelte:window on:keydown={handleEscape} />

{#if visible && hasIssues}
	<!-- Backdrop -->
	<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-xs" role="presentation">
		<!-- Modal -->
		<div class="bg-dark-900 border border-dark-700/50 rounded-2xl shadow-2xl w-full max-w-lg mx-4 overflow-hidden" role="dialog" aria-modal="true" aria-labelledby="service-health-dialog-title">
			<!-- Header -->
			<div class="flex items-center gap-3 px-6 py-4 border-b border-dark-700/50">
				<span class="text-2xl">⚠️</span>
				<div>
					<h2 id="service-health-dialog-title" class="text-lg font-bold text-dark-100">Service Connectivity Issues</h2>
					<p class="text-xs text-dark-500">
						{issues.length} service{issues.length > 1 ? 's' : ''} need{issues.length === 1 ? 's' : ''} attention
					</p>
				</div>
			</div>

			<!-- Issue List -->
			<div class="px-6 py-4 space-y-3 max-h-48 overflow-y-auto">
				{#each issues as svc}
					<div class="flex items-start gap-3 p-3 rounded-lg bg-dark-800/50">
						<HealthBadge status={svc.status} compact />
						<div class="flex-1 min-w-0">
							<p class="text-sm font-medium text-dark-200 capitalize">{svc.name.replace('_', ' ')}</p>
							<p class="text-xs text-dark-500 mt-0.5">{svc.message}</p>
						</div>
					</div>
				{/each}
			</div>

			<!-- How to fix: Helm values (configuration as code) -->
			{#if fixes.length > 0}
				<div class="px-6 py-4 border-t border-dark-700/50 space-y-3 max-h-64 overflow-y-auto">
					<p class="text-xs text-dark-400">
						Data sources are set in your Helm values (or Secrets), then applied with
						<code>helm upgrade</code> or your GitOps tool:
					</p>
					{#each fixes as fix (fix.svc.name)}
						<div>
							<p class="text-xs font-medium text-dark-300 mb-1">{fix.snippet.label}</p>
							<pre class="text-[11px] leading-relaxed bg-dark-950 border border-dark-700/50 rounded-lg p-2 overflow-x-auto text-dark-200"><code>{fix.snippet.helm}</code></pre>
						</div>
					{/each}
				</div>
			{/if}

			<!-- Actions -->
			<div class="flex items-center justify-end gap-3 px-6 py-4 border-t border-dark-700/50">
				<a href="/settings" class="btn-secondary text-sm" on:click={dismiss}>Open settings</a>
				<button type="button" on:click={dismiss} class="btn-primary text-sm">Dismiss</button>
			</div>
		</div>
	</div>
{/if}
