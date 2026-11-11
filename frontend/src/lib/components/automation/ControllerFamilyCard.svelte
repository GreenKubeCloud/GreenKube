<script>
	/**
	 * One card per GitOps controller family (Argo CD, Flux) with an overall status
	 * and a compact list of the family's resource kinds.
	 */

	/** @type {import('./gitopsControllers.js').ControllerFamily} */
	export let family;

	const FAMILY_TONES = {
		detected: 'bg-green-500/10 text-green-400 border-green-700/30',
		degraded: 'bg-yellow-500/10 text-yellow-300 border-yellow-700/30',
		not_detected: 'bg-dark-800 text-dark-300 border-dark-600/30',
		unknown: 'bg-dark-800 text-dark-400 border-dark-600/30'
	};

	const KIND_DOTS = {
		available: 'bg-green-500',
		unavailable: 'bg-dark-500',
		unknown: 'border border-dark-500 bg-transparent'
	};

	const KIND_TEXT = {
		available: 'text-green-400',
		unavailable: 'text-dark-400',
		unknown: 'text-dark-500'
	};

	$: headingId = `gitops-family-${family.id}`;
	$: tone = FAMILY_TONES[family.status] ?? FAMILY_TONES.unknown;
</script>

<article
	class="flex h-full min-w-0 flex-col rounded-xl border border-dark-700/50 bg-dark-950/40 p-4"
	aria-labelledby={headingId}
	data-testid="controller-family-card"
	data-family={family.id}
>
	<div class="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
		<div class="min-w-0">
			<h3 id={headingId} class="text-base font-semibold text-dark-100">{family.label}</h3>
			<p class="mt-0.5 text-xs text-dark-500">{family.description}</p>
		</div>
		<span
			class="shrink-0 rounded-full border px-2.5 py-1 text-xs font-medium {tone}"
			aria-label={`${family.label} status: ${family.statusLabel}`}
		>
			{family.statusLabel}
		</span>
	</div>

	<p class="mt-3 text-xs text-dark-400">
		{#if family.status === 'degraded'}
			{family.summary}.
			<span class="text-yellow-300">Unavailable: {family.missingKinds.join(', ')}.</span>
			Workloads reconciled through these kinds cannot be bound to Git.
		{:else if family.status === 'not_detected'}
			No {family.label} custom resource definitions were found in this cluster.
		{:else if family.status === 'unknown'}
			Availability could not be determined from the latest discovery status.
		{:else if family.totalCount === 1}
			{family.groups[0].kinds[0].label} CRD detected.
		{:else}
			All {family.totalCount} resource kinds are available.
		{/if}
	</p>

	<div class="mt-4 space-y-3">
		{#each family.groups as group, groupIndex (group.label)}
			{@const groupHeadingId = `${headingId}-group-${groupIndex}`}
			<div>
				<h4 id={groupHeadingId} class="text-[11px] font-medium uppercase tracking-wide text-dark-500">
					{group.label}
				</h4>
				<ul
					class="mt-1.5 divide-y divide-dark-800 rounded-lg border border-dark-800"
					aria-labelledby={groupHeadingId}
				>
					{#each group.kinds as kind (kind.key)}
						<li
							class="flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 px-3 py-2 text-sm"
							data-kind={kind.key}
						>
							<span class="flex min-w-0 items-center gap-2">
								<span
									class="h-2 w-2 shrink-0 rounded-full {KIND_DOTS[kind.status]}"
									aria-hidden="true"
								></span>
								<span class="break-words text-dark-200">{kind.label}</span>
							</span>
							<span class="text-xs {KIND_TEXT[kind.status]}">
								{kind.statusLabel}{#if kind.note}<span class="text-dark-500">{` · ${kind.note}`}</span>{/if}
							</span>
						</li>
					{/each}
				</ul>
			</div>
		{/each}
	</div>
</article>
