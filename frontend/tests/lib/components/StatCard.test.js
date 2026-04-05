/**
 * Tests for the StatCard Svelte component.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import StatCard from '$lib/components/StatCard.svelte';


describe('StatCard', () => {
	it('renders label and value', () => {
		render(StatCard, { props: { label: 'Total CO₂', value: '142.5 g' } });
		expect(screen.getByText('Total CO₂')).toBeInTheDocument();
		expect(screen.getByText('142.5 g')).toBeInTheDocument();
	});

	it('renders icon when provided', () => {
		render(StatCard, { props: { label: 'Cost', value: '$1.23', icon: '💰' } });
		expect(screen.getByText('💰')).toBeInTheDocument();
	});

	it('renders trend when provided', () => {
		render(StatCard, { props: { label: 'CO₂', value: '50g', trend: '+12%' } });
		expect(screen.getByText('+12%')).toBeInTheDocument();
	});

	it('does not render icon when not provided', () => {
		const { container } = render(StatCard, { props: { label: 'Test', value: '0' } });
		// No span with emoji class
		const spans = container.querySelectorAll('.text-xl');
		expect(spans.length).toBe(0);
	});

	it('does not render trend when not provided', () => {
		const { container } = render(StatCard, { props: { label: 'Test', value: '0' } });
		const trendSpans = container.querySelectorAll('.text-xs.text-dark-400');
		expect(trendSpans.length).toBe(0);
	});
});
