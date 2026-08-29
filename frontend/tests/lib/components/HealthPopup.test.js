/**
 * Tests for the HealthPopup Svelte component.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import HealthPopup from '$lib/components/HealthPopup.svelte';

vi.mock('$lib/api.js', () => ({
	updateServiceConfig: vi.fn().mockResolvedValue({})
}));

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

	it('hides the Electricity Maps token field when EM is inactive', () => {
		const services = {
			...healthyServices,
			prometheus: { name: 'prometheus', status: 'unreachable', inactive: false }
		};
		render(HealthPopup, { props: { services, visible: true } });
		expect(screen.queryByText('Electricity Maps Token')).not.toBeInTheDocument();
		expect(screen.queryByLabelText('Electricity Maps Token')).not.toBeInTheDocument();
	});

	it('hides the Wattnet credential fields when Wattnet is inactive', () => {
		const services = {
			...healthyServices,
			prometheus: { name: 'prometheus', status: 'unreachable', inactive: false },
			electricity_maps: { name: 'electricity_maps', status: 'unconfigured', inactive: false },
			wattnet: { name: 'wattnet', status: 'unconfigured', inactive: true, message: 'not the active provider' }
		};
		render(HealthPopup, { props: { services, visible: true } });
		expect(screen.queryByText('Wattnet Email')).not.toBeInTheDocument();
		expect(screen.queryByLabelText('Wattnet Email')).not.toBeInTheDocument();
	});

	it('shows the Electricity Maps token field when EM is the active provider', () => {
		const services = {
			...healthyServices,
			prometheus: { name: 'prometheus', status: 'unreachable', inactive: false },
			electricity_maps: { name: 'electricity_maps', status: 'unconfigured', inactive: false }
		};
		render(HealthPopup, { props: { services, visible: true } });
		expect(screen.getByText('Electricity Maps Token')).toBeInTheDocument();
	});
});
