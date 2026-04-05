import { sveltekit } from '@sveltejs/kit/vite';
import { svelteTesting } from '@testing-library/svelte/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit(), svelteTesting()],
	server: {
		proxy: {
			'/api': {
				target: 'http://localhost:8000',
				changeOrigin: true
			}
		}
	},
	test: {
		include: ['tests/**/*.test.js'],
		environment: 'jsdom',
		globals: true,
		setupFiles: ['tests/setup.js'],
		alias: {
			'$lib': '/src/lib',
			'$app/stores': '/tests/mocks/app-stores.js'
		}
	}
});
