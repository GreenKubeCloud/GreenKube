<script>
	/**
	 * Footprint (K7): the visibility pages — footprint charts, nodes and the
	 * report builder with its CSV and JSON exports — as supporting context for
	 * the savings loop. They are the footprint analysis surface.
	 */
	import { page } from '$app/state';
	import { FOOTPRINT_SECTIONS, isFootprintSectionActive } from './sections.js';
</script>

<div class="px-4 pt-4 sm:px-6 lg:px-8 max-w-[1600px] mx-auto">
	<nav aria-label="Footprint sections" data-testid="footprint-nav">
		<ul class="flex flex-wrap gap-1 border-b border-dark-700/50">
			{#each FOOTPRINT_SECTIONS as section (section.href)}
				{@const active = isFootprintSectionActive(section.href, page?.url?.pathname ?? '')}
				<li>
					<a
						href={section.href}
						aria-current={active ? 'page' : undefined}
						class="-mb-px inline-block border-b-2 px-3 py-2 text-sm transition-colors
						       {active ? 'border-green-500 text-green-400 font-medium' : 'border-transparent text-dark-400 hover:text-dark-200'}"
					>
						{section.label}
					</a>
				</li>
			{/each}
		</ul>
	</nav>
</div>

<slot />
