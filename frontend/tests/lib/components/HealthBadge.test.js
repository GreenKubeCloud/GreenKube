/**
 * Tests for the HealthBadge Svelte component.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import HealthBadge from '$lib/components/HealthBadge.svelte';


describe('HealthBadge', () => {
	it('renders healthy status label', () => {
		render(HealthBadge, { props: { status: 'healthy' } });
		// "Healthy" appears twice in non-compact mode (label span + badge span)
		const elements = screen.getAllByText('Healthy');
		expect(elements.length).toBeGreaterThanOrEqual(1);
	});

	it('renders degraded status label', () => {
		render(HealthBadge, { props: { status: 'degraded' } });
		const elements = screen.getAllByText('Degraded');
		expect(elements.length).toBeGreaterThanOrEqual(1);
	});

	it('renders unreachable status label', () => {
		render(HealthBadge, { props: { status: 'unreachable' } });
		const elements = screen.getAllByText('Unreachable');
		expect(elements.length).toBeGreaterThanOrEqual(1);
	});

	it('renders unconfigured status label', () => {
		render(HealthBadge, { props: { status: 'unconfigured' } });
		const elements = screen.getAllByText('Not Configured');
		expect(elements.length).toBeGreaterThanOrEqual(1);
	});

	it('renders label when provided', () => {
		render(HealthBadge, { props: { status: 'healthy', label: 'Prometheus' } });
		expect(screen.getByText('Prometheus')).toBeInTheDocument();
	});

	it('renders compact mode (dot + optional label)', () => {
		const { container } = render(HealthBadge, { props: { status: 'healthy', compact: true, label: 'API' } });
		expect(screen.getByText('API')).toBeInTheDocument();
		// In compact mode, the status badge text is NOT shown
		expect(screen.queryByText('Healthy')).not.toBeInTheDocument();
	});

	it('renders non-compact mode with badge', () => {
		const { container } = render(HealthBadge, { props: { status: 'healthy' } });
		// Should have a badge element with the class containing 'badge'
		const badge = container.querySelector('.badge');
		expect(badge).not.toBeNull();
		expect(badge.textContent).toContain('Healthy');
	});

	it('renders correct dot color for each status', () => {
		const { container, unmount } = render(HealthBadge, { props: { status: 'healthy' } });
		expect(container.querySelector('.bg-green-500')).not.toBeNull();
		unmount();

		const { container: c2, unmount: u2 } = render(HealthBadge, { props: { status: 'unreachable' } });
		expect(c2.querySelector('.bg-red-500')).not.toBeNull();
		u2();

		const { container: c3, unmount: u3 } = render(HealthBadge, { props: { status: 'unconfigured' } });
		expect(c3.querySelector('.bg-dark-500')).not.toBeNull();
		u3();
	});

	it('applies tooltip from title attribute', () => {
		const { container } = render(HealthBadge, { props: { status: 'healthy', tooltip: 'All good' } });
		const wrapper = container.querySelector('[title="All good"]');
		expect(wrapper).not.toBeNull();
	});
});
