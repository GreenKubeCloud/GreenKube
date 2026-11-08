/**
 * Tests for the HealthPopup Svelte component.
 *
 * Data sources are configuration as code (A13, BUG-11, BUG-60): the popup
 * explains the Helm values to set and never edits them.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import HealthPopup from '$lib/components/HealthPopup.svelte';

const healthyServices = {
	prometheus: { name: 'prometheus', status: 'healthy', inactive: false },
	opencost: { name: 'opencost', status: 'healthy', inactive: false },
	electricity_maps: { name: 'electricity_maps', status: 'unconfigured', inactive: true, message: 'not the active provider' },
	wattnet: { name: 'wattnet', status: 'healthy', inactive: false }
};

describe('HealthPopup', () => {
	it('does not render when all active services are healthy', () => {
		const { container } = render(HealthPopup, { props: { services: healthyServices, visible: true } });
		expect(container.textContent.trim()).toBe('');
	});

	it('lists only non-inactive services as issues', () => {
		const services = {
			...healthyServices,
			prometheus: { name: 'prometheus', status: 'unreachable', inactive: false }
		};
		render(HealthPopup, { props: { services, visible: true } });
		expect(screen.getByText('Service Connectivity Issues')).toBeInTheDocument();
		expect(screen.getByText('prometheus')).toBeInTheDocument();
		expect(screen.queryByText('electricity maps')).not.toBeInTheDocument();
	});

	it('shows the Helm values that fix an issue instead of an editable form', () => {
		const services = {
			...healthyServices,
			prometheus: { name: 'prometheus', status: 'unreachable', inactive: false },
			electricity_maps: { name: 'electricity_maps', status: 'unconfigured', inactive: false }
		};
		const { container } = render(HealthPopup, { props: { services, visible: true } });

		expect(container.querySelectorAll('input').length).toBe(0);
		expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
		const snippets = [...container.querySelectorAll('pre code')].map((node) => node.textContent);
		expect(snippets.some((text) => text.includes('prometheus:') && text.includes('url:'))).toBe(true);
		expect(snippets.some((text) => text.includes('electricityMapsToken'))).toBe(true);
		expect(screen.getByRole('link', { name: 'Open settings' })).toHaveAttribute('href', '/settings');
	});

	it('shows no snippet for an inactive provider', () => {
		const services = {
			...healthyServices,
			prometheus: { name: 'prometheus', status: 'unreachable', inactive: false },
			wattnet: { name: 'wattnet', status: 'unconfigured', inactive: true, message: 'not the active provider' }
		};
		const { container } = render(HealthPopup, { props: { services, visible: true } });
		const snippets = [...container.querySelectorAll('pre code')].map((node) => node.textContent).join('\n');
		expect(snippets).not.toContain('wattnetEmail');
	});
});
