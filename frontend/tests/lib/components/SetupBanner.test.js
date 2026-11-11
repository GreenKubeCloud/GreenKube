import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import SetupBanner from '$lib/components/SetupBanner.svelte';

describe('SetupBanner', () => {
	it('renders nothing while the status is unknown or complete', () => {
		const { unmount } = render(SetupBanner, { props: { status: null } });
		expect(screen.queryByTestId('setup-banner')).not.toBeInTheDocument();
		unmount();
		render(SetupBanner, { props: { status: { complete: true, actionCount: 0 } } });
		expect(screen.queryByTestId('setup-banner')).not.toBeInTheDocument();
	});

	it('counts the checks that need attention', () => {
		render(SetupBanner, { props: { status: { complete: false, actionCount: 1 } } });
		expect(screen.getByRole('region', { name: 'Setup status' })).toHaveTextContent('1 check needs your attention');
	});

	it('says when only pending checks remain', () => {
		render(SetupBanner, { props: { status: { complete: false, actionCount: 0 } } });
		expect(screen.getByTestId('setup-banner')).toHaveTextContent('Some checks are still pending.');
	});
});
