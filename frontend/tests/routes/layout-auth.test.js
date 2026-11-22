/**
 * Sign in once (F14, K11): the layout asks /auth/me at load, shows a
 * password-manager friendly sign-in form only on 401, forces the first-login
 * password change, offers an account menu and warns about plain HTTP.
 *
 * Uses the real API client with a mocked fetch that emulates the server.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import Layout from '../../src/routes/+layout.svelte';
import { clearCsrfToken, getConfig } from '#lib/api.js';
import { currentUser } from '#lib/stores.js';
import { getDisplayCurrency, setDisplayCurrency } from '#lib/utils/format.js';
import { createMemoryStorage, installStorage } from '../helpers/storage.js';

const ADMIN = {
	authenticated: true,
	kind: 'user',
	name: 'admin',
	actor: 'user:admin',
	role: 'admin',
	scopes: ['admin', 'read', 'write'],
	must_change_password: false,
	csrf_token: 'csrf-1'
};

function ok(body, status = 200) {
	return { ok: true, status, json: () => Promise.resolve(body) };
}

function fail(status, body = {}) {
	return { ok: false, status, json: () => Promise.resolve(body) };
}

/** A tiny server: one optional signed-in principal behind a cookie. */
function installServer({ principal = null, mustChange = false } = {}) {
	const state = { principal, mustChange };
	globalThis.fetch = vi.fn((url, options = {}) => {
		const path = new URL(url).pathname;
		const method = options.method || 'GET';
		if (path === '/api/v1/health') return Promise.resolve(ok({ status: 'ok', version: '0.4.0' }));
		// Public, like the real endpoint.
		if (path === '/api/v1/auth/providers') return Promise.resolve(ok({ mode: 'session', password: true, api_key: false, sso: null }));
		if (path === '/api/v1/auth/login') {
			const body = JSON.parse(options.body);
			if (body.password !== 'correct-password') {
				return Promise.resolve(fail(401, { detail: { code: 'INVALID_CREDENTIALS', message: 'Invalid username or password.' } }));
			}
			state.principal = { ...ADMIN, must_change_password: state.mustChange };
			return Promise.resolve(ok(state.principal));
		}
		if (path === '/api/v1/auth/logout') {
			state.principal = null;
			return Promise.resolve({ ok: true, status: 204, json: () => Promise.reject(new Error('empty')) });
		}
		if (!state.principal) return Promise.resolve(fail(401, { detail: 'Authentication required.' }));
		if (path === '/api/v1/auth/me') return Promise.resolve(ok(state.principal));
		if (path === '/api/v1/auth/password' && method === 'POST') {
			state.principal = { ...state.principal, must_change_password: false, csrf_token: 'csrf-2' };
			return Promise.resolve(ok({ ...state.principal, initial_admin_secret_erased: true }));
		}
		if (path === '/api/v1/health/services') return Promise.resolve(ok({ status: 'ok', services: {} }));
		return Promise.resolve(ok({}));
	});
	return state;
}

let restore = [];
let sessionStorage;
let localStorage;
const originalLocation = window.location;

function setLocation(href) {
	const url = new URL(href);
	Object.defineProperty(window, 'location', {
		configurable: true,
		value: { ...originalLocation, href: url.href, origin: url.origin, protocol: url.protocol, hostname: url.hostname }
	});
}

beforeEach(() => {
	sessionStorage = createMemoryStorage();
	localStorage = createMemoryStorage();
	restore = [installStorage('sessionStorage', sessionStorage), installStorage('localStorage', localStorage)];
	clearCsrfToken();
	currentUser.set(null);
});

afterEach(() => {
	Object.defineProperty(window, 'location', { configurable: true, value: originalLocation });
	clearCsrfToken();
	restore.reverse().forEach((undo) => undo());
	vi.restoreAllMocks();
});

async function findLoginForm() {
	return screen.findByRole('form', { name: 'Sign in to GreenKube' });
}

describe('sign-in', () => {
	it('shows a password-manager friendly form only when /auth/me answers 401', async () => {
		installServer();
		render(Layout);

		const form = await findLoginForm();
		const username = within(form).getByLabelText('Username');
		const password = within(form).getByLabelText('Password');
		expect(username).toHaveAttribute('autocomplete', 'username');
		expect(username).toHaveAttribute('name', 'username');
		expect(password).toHaveAttribute('type', 'password');
		expect(password).toHaveAttribute('autocomplete', 'current-password');
		expect(password).toHaveAttribute('name', 'password');
		expect(within(form).getByLabelText('Keep me signed in')).toHaveAttribute('type', 'checkbox');
		expect(within(form).getByRole('button', { name: 'Sign in' })).toHaveAttribute('type', 'submit');
		expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/auth/me'), expect.anything());
	});

	it('does not ask again when a session cookie is already valid (reload, new tab)', async () => {
		installServer({ principal: ADMIN });
		render(Layout);

		expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument();
		expect(screen.queryByRole('form', { name: 'Sign in to GreenKube' })).not.toBeInTheDocument();
		expect(screen.getByTestId('account-menu')).toHaveTextContent('admin');
		expect(screen.getByRole('link', { name: 'API keys' })).toHaveAttribute('href', '/settings/api-keys');
		expect(sessionStorage.length).toBe(0);
		expect(localStorage.length).toBe(0);
	});

	it('signs in and enters the application', async () => {
		installServer();
		render(Layout);
		const form = await findLoginForm();

		await fireEvent.input(within(form).getByLabelText('Username'), { target: { value: 'admin' } });
		await fireEvent.input(within(form).getByLabelText('Password'), { target: { value: 'correct-password' } });
		await fireEvent.submit(form);

		expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument();
		const loginCall = fetch.mock.calls.find(([url]) => url.includes('/api/v1/auth/login'));
		expect(JSON.parse(loginCall[1].body)).toEqual({ username: 'admin', password: 'correct-password', remember: false });
	});

	it('reports rejected credentials', async () => {
		installServer();
		render(Layout);
		const form = await findLoginForm();

		await fireEvent.input(within(form).getByLabelText('Username'), { target: { value: 'admin' } });
		await fireEvent.input(within(form).getByLabelText('Password'), { target: { value: 'wrong' } });
		await fireEvent.submit(form);

		expect(await within(form).findByRole('alert')).toHaveTextContent('Invalid username or password.');
	});

	it('forces the first-login password change before the application', async () => {
		installServer({ mustChange: true });
		render(Layout);
		const form = await findLoginForm();
		await fireEvent.input(within(form).getByLabelText('Username'), { target: { value: 'admin' } });
		await fireEvent.input(within(form).getByLabelText('Password'), { target: { value: 'correct-password' } });
		await fireEvent.submit(form);

		const change = await screen.findByRole('form', { name: 'Change password' });
		expect(within(change).getByLabelText('New password')).toHaveAttribute('autocomplete', 'new-password');
		expect(screen.queryByTestId('account-menu')).not.toBeInTheDocument();

		await fireEvent.input(within(change).getByLabelText('Current password'), { target: { value: 'correct-password' } });
		await fireEvent.input(within(change).getByLabelText('New password'), { target: { value: 'a-new-password-123' } });
		await fireEvent.input(within(change).getByLabelText('Confirm new password'), { target: { value: 'a-new-password-123' } });
		await fireEvent.submit(change);

		expect(await screen.findByTestId('account-menu')).toHaveTextContent('admin');
		expect(screen.getByText(/initial admin password was erased/)).toBeInTheDocument();
		const passwordCall = fetch.mock.calls.find(([url]) => url.includes('/api/v1/auth/password'));
		expect(passwordCall[1].headers['X-CSRF-Token']).toBe('csrf-1');
	});

	it('signs out server-side', async () => {
		const state = installServer({ principal: ADMIN });
		render(Layout);

		await fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));

		expect(await findLoginForm()).toBeInTheDocument();
		expect(state.principal).toBeNull();
		const logoutCall = fetch.mock.calls.find(([url]) => url.includes('/api/v1/auth/logout'));
		expect(logoutCall[1].method).toBe('POST');
	});

	it('returns to the sign-in form when the session later expires', async () => {
		const state = installServer({ principal: ADMIN });
		render(Layout);
		await screen.findByRole('button', { name: 'Sign out' });

		state.principal = null;
		await expect(getConfig()).rejects.toMatchObject({ status: 401 });

		expect(await findLoginForm()).toBeInTheDocument();
	});
});

describe('pricing currency', () => {
	it('reads the pricing currency from /config before the pages render', async () => {
		setDisplayCurrency('USD');
		installServer({ principal: ADMIN });
		const serve = globalThis.fetch;
		globalThis.fetch = vi.fn((url, options) =>
			new URL(url).pathname === '/api/v1/config' ? Promise.resolve(ok({ currency: 'EUR' })) : serve(url, options)
		);
		render(Layout);

		expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument();
		expect(getDisplayCurrency()).toBe('EUR');
		setDisplayCurrency('USD');
	});

	it('keeps USD when the server has no currency field', async () => {
		setDisplayCurrency('USD');
		installServer({ principal: ADMIN });
		render(Layout);

		expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument();
		expect(getDisplayCurrency()).toBe('USD');
	});
});

describe('TLS warning', () => {
	it('warns on plain HTTP from a remote host', async () => {
		setLocation('http://greenkube.example.com/');
		installServer();
		render(Layout);

		await findLoginForm();
		expect(screen.getByTestId('tls-warning')).toHaveTextContent('not encrypted');
	});

	it.each(['http://localhost:8000/', 'http://127.0.0.1:8000/', 'https://greenkube.example.com/'])(
		'does not warn for %s',
		async (href) => {
			setLocation(href);
			installServer();
			render(Layout);

			await findLoginForm();
			expect(screen.queryByTestId('tls-warning')).not.toBeInTheDocument();
		}
	);
});

describe('browser signed in with an API key (api_key mode, F-09)', () => {
	const KEY_PRINCIPAL = {
		authenticated: true,
		kind: 'apikey',
		name: 'legacy',
		actor: 'api-key:digest',
		role: 'admin',
		scopes: ['admin', 'read', 'write'],
		must_change_password: false,
		csrf_token: 'csrf-key',
		auth_mode: 'api_key'
	};

	it('offers the account link and sign out, which clears the cookie', async () => {
		const state = installServer({ principal: KEY_PRINCIPAL });
		render(Layout);

		const signOut = await screen.findByRole('button', { name: 'Sign out' });
		expect(screen.getByRole('link', { name: 'Account' })).toHaveAttribute('href', '/settings/account');
		await fireEvent.click(signOut);

		await waitFor(() => expect(state.principal).toBeNull());
		expect(fetch).toHaveBeenCalledWith(
			expect.stringContaining('/api/v1/auth/logout'),
			expect.objectContaining({ method: 'POST' })
		);
	});
});
