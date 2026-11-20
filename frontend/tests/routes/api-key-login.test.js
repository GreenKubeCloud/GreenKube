/**
 * Browser sign-in with an API key when API_AUTH_MODE=api_key (roadmap A.6,
 * field report F-09): the form shows an API-key field instead of nothing, and
 * the key is exchanged for an HttpOnly cookie, never kept by the client.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import LoginForm from '../../src/lib/components/LoginForm.svelte';

function ok(body, status = 200) {
	return { ok: true, status, json: () => Promise.resolve(body) };
}

function fail(status, body = {}) {
	return { ok: false, status, json: () => Promise.resolve(body) };
}

function installFetch(routes) {
	globalThis.fetch = vi.fn((url, options = {}) => {
		const parsed = new URL(url, 'http://localhost:3000');
		const key = `${options.method || 'GET'} ${parsed.pathname}`;
		const handler = routes[key];
		if (!handler) return Promise.resolve(fail(404, { detail: 'Not Found' }));
		return Promise.resolve(handler(parsed, options));
	});
}

const API_KEY_PROVIDERS = { mode: 'api_key', password: false, api_key: true, sso: null };
const PRINCIPAL = {
	authenticated: true,
	kind: 'apikey',
	name: 'legacy',
	scopes: ['admin'],
	csrf_token: 'csrf-1',
	auth_mode: 'api_key'
};

afterEach(() => {
	vi.restoreAllMocks();
});

describe('sign-in form in api_key mode', () => {
	it('shows an API-key field and no username or password field', async () => {
		installFetch({ 'GET /api/v1/auth/providers': () => ok(API_KEY_PROVIDERS) });
		render(LoginForm);

		expect(await screen.findByLabelText('API key')).toBeInTheDocument();
		expect(screen.queryByLabelText('Username')).not.toBeInTheDocument();
		expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
		expect(screen.queryByText(/initial admin password/i)).not.toBeInTheDocument();
	});

	it('signs in with the key and never stores it', async () => {
		let sentBody = null;
		installFetch({
			'GET /api/v1/auth/providers': () => ok(API_KEY_PROVIDERS),
			'POST /api/v1/auth/api-key-login': (_url, options) => {
				sentBody = JSON.parse(options.body);
				return ok(PRINCIPAL);
			}
		});
		const setItem = vi.spyOn(Storage.prototype, 'setItem');
		const signedIn = vi.fn();
		render(LoginForm, { events: { signedin: (event) => signedIn(event.detail) } });

		const field = await screen.findByLabelText('API key');
		expect(field).toHaveAttribute('type', 'password');
		await fireEvent.input(field, { target: { value: 'gk_secret' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

		await waitFor(() => expect(signedIn).toHaveBeenCalledWith(PRINCIPAL));
		expect(sentBody).toEqual({ key: 'gk_secret', remember: false });
		expect(setItem).not.toHaveBeenCalled();
	});

	it('explains a refused key', async () => {
		installFetch({
			'GET /api/v1/auth/providers': () => ok(API_KEY_PROVIDERS),
			'POST /api/v1/auth/api-key-login': () => fail(401, { detail: 'Invalid or missing API key.' })
		});
		render(LoginForm);

		await fireEvent.input(await screen.findByLabelText('API key'), { target: { value: 'wrong' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

		expect(await screen.findByRole('alert')).toHaveTextContent('Invalid API key.');
	});
});

describe('sign-in form in session mode', () => {
	it('keeps the password form and the initial-password hint', async () => {
		installFetch({
			'GET /api/v1/auth/providers': () => ok({ mode: 'session', password: true, api_key: false, sso: null })
		});
		render(LoginForm);

		await waitFor(() => expect(screen.getByText(/initial admin password/i)).toBeInTheDocument());
		expect(screen.getByLabelText('Username')).toBeInTheDocument();
		expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
	});
});
