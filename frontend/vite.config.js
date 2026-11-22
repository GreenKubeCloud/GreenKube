import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { svelteTesting } from '@testing-library/svelte/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		// SvelteKit 3 reads its configuration from the plugin options (the
		// former svelte.config.js). The SPA is served by the Python API from
		// build/, with index.html as the fallback for client-side routes.
		sveltekit({
			preprocess: vitePreprocess(),
			adapter: adapter({
				pages: 'build',
				assets: 'build',
				fallback: 'index.html',
				precompress: true,
				strict: true
			}),
			paths: {
				base: ''
			}
		}),
		svelteTesting()
	],
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
			'$app/state': '/tests/mocks/app-state.js'
		}
	}
});
