<script>
	/** Status word shown as text (never colour-only), with an optional icon. */
	export let label = '';
	/** @type {'green'|'yellow'|'red'|'blue'|'purple'|'dark'} */
	export let tone = 'dark';
	export let icon = '';

	const TONES = {
		green: 'bg-green-900/40 text-green-300 border-green-700/40',
		yellow: 'bg-yellow-900/40 text-yellow-300 border-yellow-700/40',
		red: 'bg-red-900/40 text-red-300 border-red-700/40',
		blue: 'bg-blue-900/40 text-blue-300 border-blue-700/40',
		purple: 'bg-purple-900/40 text-purple-300 border-purple-700/40',
		dark: 'bg-dark-800 text-dark-300 border-dark-600/50'
	};
</script>

<span
	class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium whitespace-nowrap {TONES[tone] ?? TONES.dark}"
>
	{#if icon}<span aria-hidden="true">{icon}</span>{/if}
	<span>{label}</span>
</span>
