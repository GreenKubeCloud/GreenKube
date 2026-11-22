/**
 * Manual mode (H9): kubectl steps on demand and "Mark as applied".
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import ManualApplyPanel from '../../../src/lib/components/ManualApplyPanel.svelte';
import * as api from '#lib/api.js';
import { currentUser } from '#lib/stores.js';

vi.mock('#lib/api.js', () => ({
	getManualGuide: vi.fn(),
	markAppliedManually: vi.fn()
}));

const deleteGuide = {
	recommendation_id: 6,
	mode: 'delete',
	action_kind: 'delete_resource',
	target: { api_version: 'v1', kind: 'PersistentVolume', namespace: '', name: 'pv-orphan' },
	commands: [
		{ description: 'Check what would be deleted (server-side dry run).', command: 'kubectl delete persistentvolume pv-orphan --dry-run=server' },
		{ description: 'Delete PersistentVolume pv-orphan.', command: 'kubectl delete persistentvolume pv-orphan' }
	],
	patch: null,
	diff: null,
	notes: ['Deleting cannot be undone.'],
	can_mark_applied: true
};

describe('ManualApplyPanel', () => {
	beforeEach(() => {
		vi.mocked(api.getManualGuide).mockReset();
		vi.mocked(api.markAppliedManually).mockReset();
	});

	it('loads the steps only on demand', async () => {
		vi.mocked(api.getManualGuide).mockResolvedValue(deleteGuide);
		render(ManualApplyPanel, { props: { recommendationId: 6 } });
		expect(api.getManualGuide).not.toHaveBeenCalled();

		await fireEvent.click(screen.getByRole('button', { name: 'Show manual steps' }));

		expect(api.getManualGuide).toHaveBeenCalledWith(6);
		expect(await screen.findByText('kubectl delete persistentvolume pv-orphan')).toBeTruthy();
		expect(screen.getByText(/cannot be undone/)).toBeTruthy();
	});

	it('shows a viewer the steps without "Mark as applied" (a write)', async () => {
		currentUser.set({ kind: 'user', name: 'vera', role: 'viewer', scopes: ['read'] });
		vi.mocked(api.getManualGuide).mockResolvedValue(deleteGuide);
		render(ManualApplyPanel, { props: { recommendationId: 6, open: true } });

		expect(await screen.findByTestId('manual-read-only')).toBeTruthy();
		expect(screen.getByText('kubectl delete persistentvolume pv-orphan')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Mark as applied' })).toBeNull();
	});

	it('marks the recommendation applied after a confirmation', async () => {
		vi.mocked(api.getManualGuide).mockResolvedValue(deleteGuide);
		vi.mocked(api.markAppliedManually).mockResolvedValue({ id: 6, status: 'applied' });
		const applied = vi.fn();
		render(ManualApplyPanel, { props: { recommendationId: 6, open: true }, events: { applied } });

		await fireEvent.click(await screen.findByRole('button', { name: 'Mark as applied' }));
		expect(api.markAppliedManually).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Yes, mark as applied' }));

		expect(api.markAppliedManually).toHaveBeenCalledWith(6);
		expect(await screen.findByTestId('manual-applied')).toBeTruthy();
		expect(applied).toHaveBeenCalledTimes(1);
	});

	it('explains advisory changes and hides the button when it cannot be marked', async () => {
		vi.mocked(api.getManualGuide).mockResolvedValue({
			...deleteGuide,
			mode: 'advisory',
			commands: [],
			notes: [],
			can_mark_applied: false
		});
		render(ManualApplyPanel, { props: { recommendationId: 9, open: true } });

		expect(await screen.findByTestId('manual-advisory')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Mark as applied' })).toBeNull();
	});

	it('shows a failed load', async () => {
		vi.mocked(api.getManualGuide).mockRejectedValueOnce(new Error('boom'));
		render(ManualApplyPanel, { props: { recommendationId: 6, open: true } });
		expect(await screen.findByRole('alert')).toBeTruthy();
	});
});
