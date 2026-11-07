/**
 * Sign-in dialog (F13, BUG-63): a password-manager friendly form, the key kept
 * per tab in sessionStorage, and cleared on 401 or sign out.
 *
 * Uses the real API client with a mocked fetch.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import Layout from '../../src/routes/+layout.svelte';
import { API_TOKEN_STORAGE_KEY, clearApiToken, getHealth } from '$lib/api.js';
import { createMemoryStorage, installStorage } from '../helpers/storage.js';

const VALID_KEY = 'gk-valid-key';

function ok(body) {
	return { ok: true, status: 200, json: () => Promise.resolve(body) };
}

function unauthorized() {
	return { ok: false, status: 401, json: () => Promise.resolve({ detail: 'Invalid or missing API key.' }) };
}

function installFetch() {
	globalThis.fetch = vi.fn((url, options = {}) => {
		const path = new URL(url).pathname;
		if (path === '/api/v1/health') return Promise.resolve(ok({ status: 'ok', version: '0.3.1' }));
		if (options.headers?.Authorization !== `Bearer ${VALID_KEY}`) return Promise.resolve(unauthorized());
		if (path === '/api/v1/health/services') return Promise.resolve(ok({ status: 'ok', services: {} }));
		return Promise.resolve(ok({}));
	});
}

let sessionStorage;
let localStorage;
let restore = [];

beforeEach(() => {
	sessionStorage = createMemoryStorage();
	localStorage = createMemoryStorage();
	restore = [installStorage('sessionStorage', sessionStorage), installStorage('localStorage', localStorage)];
	clearApiToken();
	installFetch();
});

afterEach(() => {
	clearApiToken();
	restore.reverse().forEach((undo) => undo());
	vi.restoreAllMocks();
});

async function findDialog() {
	return screen.findByRole('dialog', { name: 'API authentication required' });
}

describe('sign-in dialog', () => {
	it('is a real form that password managers can save and autofill', async () => {
		render(Layout);
		const dialog = await findDialog();

		const form = within(dialog).getByRole('form', { name: 'Sign in with an API key' });
		const username = within(form).getByLabelText('Account');
		const key = within(form).getByLabelText('API key');
		expect(username).toHaveValue('api-key');
		expect(username).toHaveAttribute('readonly');
		expect(username).toHaveAttribute('autocomplete', 'username');
		expect(username).toHaveAttribute('name', 'username');
		expect(key).toHaveAttribute('type', 'password');
		expect(key).toHaveAttribute('autocomplete', 'current-password');
		expect(key).toHaveAttribute('name', 'password');
		expect(within(form).getByRole('button', { name: 'Connect' })).toHaveAttribute('type', 'submit');
	});

	it('keeps an accepted key for this tab only and closes the dialog', async () => {
		render(Layout);
		const dialog = await findDialog();

		await fireEvent.input(within(dialog).getByLabelText('API key'), { target: { value: ` ${VALID_KEY} ` } });
		await fireEvent.submit(within(dialog).getByRole('form'));

		await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
		expect(sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBe(VALID_KEY);
		expect(localStorage.length).toBe(0);
		expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
	});

	it('does not ask again after a reload of the same tab', async () => {
		sessionStorage.setItem(API_TOKEN_STORAGE_KEY, VALID_KEY);

		render(Layout);

		await waitFor(() =>
			expect(fetch).toHaveBeenCalledWith(
				expect.stringContaining('/api/v1/health/services'),
				expect.objectContaining({ headers: { Authorization: `Bearer ${VALID_KEY}` } })
			)
		);
		expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
		expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument();
	});

	it('reports a rejected key and does not keep it', async () => {
		render(Layout);
		const dialog = await findDialog();

		await fireEvent.input(within(dialog).getByLabelText('API key'), { target: { value: 'gk-wrong' } });
		await fireEvent.submit(within(dialog).getByRole('form'));

		expect(await within(dialog).findByRole('alert')).toHaveTextContent('The API key was rejected.');
		expect(sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
	});

	it('asks for a key before submitting an empty form', async () => {
		render(Layout);
		const dialog = await findDialog();
		const callsBefore = fetch.mock.calls.length;

		await fireEvent.submit(within(dialog).getByRole('form'));

		expect(await within(dialog).findByRole('alert')).toHaveTextContent('Enter an API key.');
		expect(fetch.mock.calls.length).toBe(callsBefore);
	});

	it('forgets the key on sign out', async () => {
		sessionStorage.setItem(API_TOKEN_STORAGE_KEY, VALID_KEY);
		render(Layout);

		await fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));

		expect(sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
		expect(await findDialog()).toBeInTheDocument();
		expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument();
	});

	it('forgets the key and asks again when the API later rejects it', async () => {
		sessionStorage.setItem(API_TOKEN_STORAGE_KEY, VALID_KEY);
		render(Layout);
		await screen.findByRole('button', { name: 'Sign out' });

		globalThis.fetch = vi.fn(() => Promise.resolve(unauthorized()));
		await expect(getHealth()).rejects.toMatchObject({ status: 401 });

		expect(await findDialog()).toBeInTheDocument();
		expect(sessionStorage.getItem(API_TOKEN_STORAGE_KEY)).toBeNull();
		expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument();
	});
});
