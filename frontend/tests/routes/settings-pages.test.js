/**
 * Settings (A13, K11): read-only data sources with Helm snippets, the account
 * page (sessions, password) and API keys shown once.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import SettingsPage from '../../src/routes/settings/+page.svelte';
import AccountPage from '../../src/routes/settings/account/+page.svelte';
import ApiKeysPage from '../../src/routes/settings/api-keys/+page.svelte';
import * as api from '$lib/api.js';
import { currentUser } from '$lib/stores.js';

vi.mock('$lib/api.js', () => ({
	ApiError: class ApiError extends Error {},
	getConfig: vi.fn(),
	getVersion: vi.fn(),
	getHealth: vi.fn(),
	getServicesHealth: vi.fn(),
	listSessions: vi.fn(),
	revokeSession: vi.fn(),
	logout: vi.fn(),
	changePassword: vi.fn(),
	listApiKeys: vi.fn(),
	createApiKey: vi.fn(),
	revokeApiKey: vi.fn(),
	listUsers: vi.fn(() => Promise.resolve([])),
	createUser: vi.fn(),
	updateUser: vi.fn()
}));

const ADMIN = { kind: 'user', name: 'admin', role: 'admin', scopes: ['admin'] };

beforeEach(() => {
	vi.clearAllMocks();
	currentUser.set(ADMIN);
	api.getConfig.mockResolvedValue({ db_type: 'postgres', log_level: 'INFO' });
	api.getVersion.mockResolvedValue({ version: '0.4.0' });
	api.getHealth.mockResolvedValue({ status: 'ok', version: '0.4.0' });
	api.getServicesHealth.mockResolvedValue({
		services: {
			prometheus: { name: 'prometheus', status: 'healthy', message: 'ok', url: 'http://prom:9090' }
		}
	});
});

describe('settings page', () => {
	it('shows data sources read-only with Helm snippets (configuration as code)', async () => {
		const { container } = render(SettingsPage);

		const snippets = await screen.findByTestId('helm-snippets');
		expect(within(snippets).getByText('Prometheus')).toBeInTheDocument();
		expect(container.querySelectorAll('input').length).toBe(0);
		expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
		expect(screen.getByText(/helm upgrade greenkube/)).toBeInTheDocument();
		expect(screen.getByRole('link', { name: 'API keys' })).toHaveAttribute('href', '/settings/api-keys');
	});

	it('hides the API keys link from non-admins', async () => {
		currentUser.set({ kind: 'user', name: 'viewer', role: 'viewer', scopes: ['read'] });
		render(SettingsPage);

		await screen.findByTestId('helm-snippets');
		expect(screen.queryByRole('link', { name: 'API keys' })).not.toBeInTheDocument();
		expect(screen.getByRole('link', { name: /Account/ })).toHaveAttribute('href', '/settings/account');
	});
});

describe('account page', () => {
	it('lists sessions and revokes another one', async () => {
		api.listSessions.mockResolvedValue([
			{ id: 'cur', current: true, user_agent: 'Mozilla/5.0 (Macintosh) Firefox/130.0', ip: '10.0.0.1', created_at: '2026-01-01T00:00:00Z', last_seen_at: '2026-01-01T00:00:00Z' },
			{ id: 'old', current: false, user_agent: 'Mozilla/5.0 (X11) Chrome/129.0', ip: '10.0.0.2', created_at: '2026-01-01T00:00:00Z', last_seen_at: '2026-01-01T00:00:00Z' }
		]);
		api.revokeSession.mockResolvedValue(null);
		render(AccountPage);

		const list = await screen.findByRole('list', { name: 'Active sessions' });
		expect(within(list).getByText('This browser')).toBeInTheDocument();
		await fireEvent.click(within(list).getByRole('button', { name: 'Revoke session old' }));

		expect(api.revokeSession).toHaveBeenCalledWith('old');
		await waitFor(() => expect(within(list).queryByRole('button', { name: 'Revoke session old' })).not.toBeInTheDocument());
	});

	it('changes the password with a password-manager friendly form', async () => {
		api.listSessions.mockResolvedValue([]);
		api.changePassword.mockResolvedValue({ ...ADMIN, csrf_token: 'x' });
		render(AccountPage);

		const form = await screen.findByRole('form', { name: 'Change password' });
		expect(within(form).getByLabelText('New password')).toHaveAttribute('autocomplete', 'new-password');
		await fireEvent.input(within(form).getByLabelText('Current password'), { target: { value: 'old-password-1' } });
		await fireEvent.input(within(form).getByLabelText('New password'), { target: { value: 'short' } });
		await fireEvent.input(within(form).getByLabelText('Confirm new password'), { target: { value: 'short' } });
		await fireEvent.submit(form);
		expect(await within(form).findByRole('alert')).toHaveTextContent('at least 12');
		expect(api.changePassword).not.toHaveBeenCalled();

		await fireEvent.input(within(form).getByLabelText('New password'), { target: { value: 'new-password-123' } });
		await fireEvent.input(within(form).getByLabelText('Confirm new password'), { target: { value: 'new-password-123' } });
		await fireEvent.submit(form);

		await waitFor(() =>
			expect(api.changePassword).toHaveBeenCalledWith({ currentPassword: 'old-password-1', newPassword: 'new-password-123' })
		);
		expect(await screen.findByText(/other sessions were signed out/)).toBeInTheDocument();
	});
});

describe('API keys page', () => {
	it('creates a scoped key and shows it once', async () => {
		api.listApiKeys.mockResolvedValue([]);
		api.createApiKey.mockResolvedValue({
			id: 'abcd1234',
			name: 'prometheus',
			prefix: 'gk_abcd1234',
			scopes: ['metrics:read'],
			key: 'gk_abcd1234_secret-value',
			active: true
		});
		render(ApiKeysPage);

		const form = await screen.findByRole('form', { name: 'Create an API key' });
		await fireEvent.input(within(form).getByLabelText('Name'), { target: { value: 'prometheus' } });
		await fireEvent.click(within(form).getByRole('checkbox', { name: /^read/ }));
		await fireEvent.click(within(form).getByRole('checkbox', { name: /metrics:read/ }));
		await fireEvent.submit(form);

		expect(api.createApiKey).toHaveBeenCalledWith({ name: 'prometheus', scopes: ['metrics:read'], expiresInDays: null });
		const shown = await screen.findByTestId('new-api-key');
		expect(shown).toHaveTextContent('gk_abcd1234_secret-value');
		expect(shown).toHaveTextContent('shown only once');

		await fireEvent.click(within(shown).getByRole('button', { name: 'I have stored the key' }));
		expect(screen.queryByText('gk_abcd1234_secret-value')).not.toBeInTheDocument();
	});

	it('lists keys without secrets and revokes one', async () => {
		api.listApiKeys.mockResolvedValue([
			{ id: 'abcd1234', name: 'ci', prefix: 'gk_abcd1234', scopes: ['read', 'write'], active: true, created_at: '2026-01-01T00:00:00Z' }
		]);
		api.revokeApiKey.mockResolvedValue(null);
		vi.spyOn(window, 'confirm').mockReturnValue(true);
		render(ApiKeysPage);

		const table = await screen.findByRole('table', { name: 'API keys' });
		expect(within(table).getByText('gk_abcd1234_…')).toBeInTheDocument();
		await fireEvent.click(within(table).getByRole('button', { name: 'Revoke ci' }));

		expect(api.revokeApiKey).toHaveBeenCalledWith('abcd1234');
	});
});
