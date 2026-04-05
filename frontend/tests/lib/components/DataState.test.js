/**
 * Tests for the DataState Svelte component.
 *
 * DataState is a conditional wrapper showing loading, error, empty,
 * or children states.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import DataState from '$lib/components/DataState.svelte';


describe('DataState', () => {
	it('shows loading spinner when loading=true', () => {
		render(DataState, { props: { loading: true } });
		expect(screen.getByText('Loading…')).toBeInTheDocument();
	});

	it('shows error message when error is set', () => {
		render(DataState, { props: { error: 'Something went wrong' } });
		expect(screen.getByText('Something went wrong')).toBeInTheDocument();
	});

	it('shows warning icon when error is set', () => {
		render(DataState, { props: { error: 'fail' } });
		expect(screen.getByText('⚠️')).toBeInTheDocument();
	});

	it('shows empty message when empty=true', () => {
		render(DataState, { props: { empty: true, emptyMessage: 'Nothing here' } });
		expect(screen.getByText('Nothing here')).toBeInTheDocument();
	});

	it('shows default empty message', () => {
		render(DataState, { props: { empty: true } });
		expect(screen.getByText('No data available')).toBeInTheDocument();
	});

	it('shows empty icon when empty=true', () => {
		render(DataState, { props: { empty: true } });
		expect(screen.getByText('📭')).toBeInTheDocument();
	});

	it('loading takes priority over error', () => {
		render(DataState, { props: { loading: true, error: 'fail' } });
		expect(screen.getByText('Loading…')).toBeInTheDocument();
		expect(screen.queryByText('fail')).not.toBeInTheDocument();
	});

	it('error takes priority over empty', () => {
		render(DataState, { props: { error: 'err', empty: true } });
		expect(screen.getByText('err')).toBeInTheDocument();
		expect(screen.queryByText('📭')).not.toBeInTheDocument();
	});
});
