<script>
	import { onMount, onDestroy } from 'svelte';
	import * as echarts from 'echarts';

	/** @type {Object} */
	export let option = {};
	/** @type {string} */
	export let height = '320px';
	/** @type {string} */
	export let className = '';

	let container;
	let chart;

	function initChart() {
		if (!container) return;
		chart = echarts.init(container, 'dark', { renderer: 'canvas' });
		chart.setOption(option);
	}

	function handleResize() {
		chart?.resize();
	}

	onMount(() => {
		initChart();
		window.addEventListener('resize', handleResize);
	});

	onDestroy(() => {
		window.removeEventListener('resize', handleResize);
		chart?.dispose();
	});

	$: if (chart && option) {
		chart.setOption(option, { notMerge: true });
	}
</script>

<div bind:this={container} class="w-full {className}" style="height: {height}"></div>
