/**
 * Tests for the Card Svelte component.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import Card from '$lib/components/Card.svelte';


describe('Card', () => {
	it('renders title when provided', () => {
		render(Card, { props: { title: 'My Card' } });
		expect(screen.getByText('My Card')).toBeInTheDocument();
	});

	it('renders icon when provided', () => {
		render(Card, { props: { title: 'Test', icon: '🌿' } });
		expect(screen.getByText('🌿')).toBeInTheDocument();
	});

	it('renders subtitle when provided', () => {
		render(Card, { props: { title: 'Test', subtitle: 'Subtitle text' } });
		expect(screen.getByText('Subtitle text')).toBeInTheDocument();
	});

	it('does not render header when no title or icon', () => {
		const { container } = render(Card, { props: {} });
		const headers = container.querySelectorAll('h3');
		expect(headers.length).toBe(0);
	});
});
