/**
 * Single sign-on and roles in the UI (F16, K11): the SSO button and its
 * errors on the sign-in form, the role and sign-in source on the account
 * page, and user administration for admins.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import LoginForm from '../../src/lib/components/LoginForm.svelte';
import AccountPage from '../../src/routes/settings/account/+page.svelte';
import { currentUser } from '$lib/stores.js';

function ok(body, status = 200) {
	return { ok: true, status, json: () => Promise.resolve(body) };
}

function fail(status, body = {}) {
	return { ok: false, status, json: () => Promise.resolve(body) };
}

function installFetch(routes) {
	globalThis.fetch = vi.fn((url, options = {}) => {
		const parsed = new URL(url);
		const key = `${options.method || 'GET'} ${parsed.pathname}`;
		const handler = routes[key];
		if (!handler) return Promise.resolve(fail(404, { detail: 'Not Found' }));
		return Promise.resolve(handler(parsed, options));
	});
}

const originalLocation = window.location;

function setLocation(path) {
	const url = new URL(path, 'http://localhost:3000');
	Object.defineProperty(window, 'location', {
		configurable: true,
		value: { href: url.href, origin: url.origin, protocol: url.protocol, hostname: url.hostname, pathname: url.pathname, search: url.search }
	});
}

beforeEach(() => {
	currentUser.set(null);
	setLocation('/recommendations');
});

afterEach(() => {
	Object.defineProperty(window, 'location', { configurable: true, value: originalLocation });
	vi.restoreAllMocks();
});

describe('sign-in form with single sign-on', () => {
	it('offers the OpenID Connect button with a return path, beside the local form', async () => {
		installFetch({
			'GET /api/v1/auth/providers': () =>
				ok({ mode: 'oidc', password: true, sso: { kind: 'oidc', name: 'Acme SSO', login_path: '/auth/oidc/login' } })
		});
		render(LoginForm);

		const button = await screen.findByTestId('sso-login');
		expect(button).toHaveTextContent('Sign in with Acme SSO');
		const href = new URL(button.getAttribute('href'));
		expect(href.pathname).toBe('/api/v1/auth/oidc/login');
		expect(href.searchParams.get('return_to')).toBe('/recommendations');
		expect(screen.getByRole('form', { name: 'Sign in to GreenKube' })).toBeInTheDocument();
		expect(screen.getByText(/or with a local account/i)).toBeInTheDocument();
	});

	it('hides the password form when local sign-in is disabled', async () => {
		installFetch({
			'GET /api/v1/auth/providers': () =>
				ok({ mode: 'oidc', password: false, sso: { kind: 'oidc', name: 'SSO', login_path: '/auth/oidc/login' } })
		});
		render(LoginForm);

		await screen.findByTestId('sso-login');
		await waitFor(() => expect(screen.queryByRole('form', { name: 'Sign in to GreenKube' })).not.toBeInTheDocument());
	});

	it('explains the authentication proxy', async () => {
		installFetch({
			'GET /api/v1/auth/providers': () =>
				ok({ mode: 'proxy', password: true, sso: { kind: 'proxy', name: 'your authentication proxy', login_path: null } })
		});
		render(LoginForm);

		expect(await screen.findByTestId('sso-proxy')).toHaveTextContent('authentication proxy');
	});

	it('shows why a single sign-on attempt failed', async () => {
		setLocation('/?sso_error=no_role');
		window.history.replaceState = vi.fn();
		installFetch({ 'GET /api/v1/auth/providers': () => ok({ mode: 'oidc', password: true, sso: null }) });
		render(LoginForm);

		expect(await screen.findByRole('alert')).toHaveTextContent('not in any group that grants access');
		expect(window.history.replaceState).toHaveBeenCalledWith(null, '', '/');
	});

	it('keeps the plain password form when providers cannot be read', async () => {
		installFetch({ 'GET /api/v1/auth/providers': () => fail(500) });
		render(LoginForm);

		expect(await screen.findByRole('form', { name: 'Sign in to GreenKube' })).toBeInTheDocument();
		expect(screen.queryByTestId('sso-login')).not.toBeInTheDocument();
	});
});

describe('account page roles', () => {
	it('shows the role from SSO groups and no password form', async () => {
		currentUser.set({ kind: 'user', name: 'alice', role: 'operator', scopes: ['read', 'write', 'approve'], auth_source: 'oidc' });
		installFetch({ 'GET /api/v1/auth/sessions': () => ok([]) });
		render(AccountPage);

		const role = await screen.findByTestId('account-role');
		expect(role).toHaveTextContent('operator');
		expect(role).toHaveTextContent('approve plans');
		expect(role).toHaveTextContent('identity provider groups');
		expect(screen.getByTestId('sso-password')).toBeInTheDocument();
		expect(screen.queryByRole('form', { name: 'Change password' })).not.toBeInTheDocument();
		expect(screen.queryByRole('table', { name: 'Users' })).not.toBeInTheDocument();
	});

	it('lets an admin change a local role, see SSO roles read-only and create a user', async () => {
		currentUser.set({ kind: 'user', name: 'admin', role: 'admin', scopes: ['admin'], auth_source: 'local' });
		const users = [
			{ id: 'u1', username: 'admin', role: 'admin', auth_source: 'local', disabled: false, role_managed_by_groups: false },
			{ id: 'u2', username: 'alice', role: 'operator', auth_source: 'oidc', disabled: false, role_managed_by_groups: true },
			{ id: 'u3', username: 'bob', role: 'viewer', auth_source: 'local', disabled: false, role_managed_by_groups: false }
		];
		const patches = [];
		const created = [];
		installFetch({
			'GET /api/v1/auth/sessions': () => ok([]),
			'GET /api/v1/auth/users': () => ok(users),
			'PATCH /api/v1/auth/users/u3': (_url, options) => {
				patches.push(JSON.parse(options.body));
				return ok({ ...users[2], role: 'operator' });
			},
			'POST /api/v1/auth/users': (_url, options) => {
				created.push(JSON.parse(options.body));
				return ok({ id: 'u4', username: 'carol', role: 'viewer', auth_source: 'local', disabled: false, role_managed_by_groups: false }, 201);
			}
		});
		render(AccountPage);

		const table = await screen.findByRole('table', { name: 'Users' });
		expect(within(table).queryByRole('combobox', { name: 'Role of alice' })).not.toBeInTheDocument();
		await fireEvent.change(within(table).getByRole('combobox', { name: 'Role of bob' }), { target: { value: 'operator' } });
		await waitFor(() => expect(patches).toEqual([{ role: 'operator' }]));
		expect(await screen.findByText('bob is now operator.')).toBeInTheDocument();

		const form = screen.getByRole('form', { name: 'Create a user' });
		await fireEvent.input(within(form).getByLabelText('Username'), { target: { value: 'carol' } });
		await fireEvent.input(within(form).getByLabelText('Initial password'), { target: { value: 'carol-initial-pass' } });
		await fireEvent.submit(form);
		await waitFor(() => expect(created).toEqual([{ username: 'carol', password: 'carol-initial-pass', role: 'viewer' }]));
		expect(await within(table).findByText('carol')).toBeInTheDocument();
	});

	it('reports the last-admin refusal', async () => {
		currentUser.set({ kind: 'user', name: 'admin', role: 'admin', scopes: ['admin'], auth_source: 'local' });
		installFetch({
			'GET /api/v1/auth/sessions': () => ok([]),
			'GET /api/v1/auth/users': () =>
				ok([{ id: 'u1', username: 'admin', role: 'admin', auth_source: 'local', disabled: false, role_managed_by_groups: false }]),
			'PATCH /api/v1/auth/users/u1': () =>
				fail(409, { detail: { code: 'LAST_ADMIN', message: 'At least one active administrator must remain.' } })
		});
		render(AccountPage);

		const table = await screen.findByRole('table', { name: 'Users' });
		await fireEvent.click(within(table).getByRole('button', { name: 'Disable admin' }));
		expect(await screen.findByText('At least one active administrator must remain.')).toBeInTheDocument();
	});
});
