<script>
	/**
	 * Sign-in form (F14, F16, K11). A real <form> with username/current-password
	 * autocomplete and a native submit, so browsers and password managers offer
	 * to save and autofill the credentials. With single sign-on configured it
	 * also offers the SSO button (OpenID Connect) or explains that the
	 * authentication proxy signs users in, and shows why an SSO attempt failed.
	 */
	import { createEventDispatcher, onMount } from 'svelte';
	import { ApiError, getAuthProviders, login, ssoLoginUrl } from '$lib/api.js';

	const dispatch = createEventDispatcher();

	/** Stable error codes from /api/v1/auth/oidc/callback (?sso_error=...). */
	const SSO_ERRORS = {
		denied: 'The identity provider did not sign you in.',
		state_mismatch: 'The sign-in started in another browser or tab. Try again.',
		state_expired: 'The sign-in took too long or was already used. Try again.',
		invalid_request: 'The identity provider answered an incomplete sign-in. Try again.',
		invalid_token: 'The identity provider sent an identity GreenKube could not verify.',
		unknown_key: 'The identity provider signed the sign-in with an unknown key.',
		token_exchange_failed: 'GreenKube could not finish the sign-in with the identity provider.',
		provider_unavailable: 'The identity provider is not reachable. Try again later.',
		no_role: 'Your account is not in any group that grants access to GreenKube. Ask an administrator.',
		account_conflict: 'Another GreenKube account already uses your username. Ask an administrator.',
		account_disabled: 'Your GreenKube account is disabled.',
		invalid_identity: 'The identity provider sent no usable identity.'
	};

	let username = '';
	let password = '';
	let remember = false;
	let error = '';
	let submitting = false;
	let providers = null;
	let ssoError = '';

	$: passwordEnabled = providers ? providers.password !== false : true;
	$: sso = providers?.sso || null;

	function readSsoError() {
		if (typeof window === 'undefined') return '';
		try {
			const params = new URLSearchParams(window.location.search);
			const code = params.get('sso_error');
			if (!code) return '';
			params.delete('sso_error');
			const query = params.toString();
			window.history?.replaceState?.(null, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
			return SSO_ERRORS[code] || 'Single sign-on failed. Try again.';
		} catch {
			return '';
		}
	}

	function returnTo() {
		if (typeof window === 'undefined') return '/';
		return `${window.location.pathname}${window.location.search}` || '/';
	}

	onMount(async () => {
		ssoError = readSsoError();
		providers = await getAuthProviders();
	});

	function messageFor(err) {
		if (err instanceof ApiError) {
			if (err.status === 429) return 'Too many attempts. Wait a minute and try again.';
			if (err.status === 401) return 'Invalid username or password.';
			if (err.status === 409) return err.message || 'Password sign-in is disabled on this instance.';
			if (err.status === 503) return 'The account store is not available yet. Try again shortly.';
		}
		return err?.message || 'Sign-in failed.';
	}

	async function submit() {
		if (!username.trim() || !password) {
			error = 'Enter your username and password.';
			return;
		}
		submitting = true;
		error = '';
		try {
			const principal = await login({ username: username.trim(), password, remember });
			password = '';
			dispatch('signedin', principal);
		} catch (err) {
			error = messageFor(err);
		} finally {
			submitting = false;
		}
	}
</script>

<div class="space-y-4">
	{#if ssoError}
		<p class="rounded-lg border border-red-800/60 bg-red-900/20 px-3 py-2 text-sm text-red-300" role="alert">
			{ssoError}
		</p>
	{/if}

	{#if sso?.kind === 'oidc'}
		<a
			href={ssoLoginUrl(sso.login_path || '/auth/oidc/login', returnTo())}
			class="flex w-full items-center justify-center gap-2 rounded-lg bg-green-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-green-500"
			data-testid="sso-login"
		>
			Sign in with {sso.name || 'SSO'}
		</a>
	{:else if sso?.kind === 'proxy'}
		<p class="text-sm text-dark-400" data-testid="sso-proxy">
			Single sign-on is handled by {sso.name || 'your authentication proxy'}: open GreenKube through it to be
			signed in automatically.
		</p>
	{/if}

	{#if passwordEnabled}
		{#if sso}
			<p class="text-center text-xs uppercase tracking-wide text-dark-500">or with a local account</p>
		{/if}
		<form
			class="space-y-4"
			method="post"
			action="/api/v1/auth/login"
			aria-label="Sign in to GreenKube"
			on:submit|preventDefault={submit}
		>
			<div class="space-y-1">
				<label for="login-username" class="block text-sm font-medium text-dark-300">Username</label>
				<input
					id="login-username"
					name="username"
					type="text"
					bind:value={username}
					autocomplete="username"
					autocapitalize="none"
					spellcheck="false"
					required
					class="w-full rounded-lg border border-dark-700 bg-dark-800 px-3 py-2 text-dark-100 outline-hidden focus:border-green-500"
				/>
			</div>
			<div class="space-y-1">
				<label for="login-password" class="block text-sm font-medium text-dark-300">Password</label>
				<input
					id="login-password"
					name="password"
					type="password"
					bind:value={password}
					autocomplete="current-password"
					required
					class="w-full rounded-lg border border-dark-700 bg-dark-800 px-3 py-2 text-dark-100 outline-hidden focus:border-green-500"
				/>
			</div>
			<label class="flex items-center gap-2 text-sm text-dark-400">
				<input name="remember" type="checkbox" bind:checked={remember} class="rounded border-dark-600" />
				Keep me signed in
			</label>
			{#if error}
				<p class="text-sm text-red-400" role="alert">{error}</p>
			{/if}
			<button
				type="submit"
				disabled={submitting}
				class="w-full rounded-lg {sso
					? 'border border-dark-600 bg-dark-800 text-dark-100 hover:bg-dark-700'
					: 'bg-green-600 text-white hover:bg-green-500'} px-4 py-2 text-sm font-medium transition disabled:cursor-wait disabled:opacity-60"
			>
				{submitting ? 'Signing in…' : 'Sign in'}
			</button>
		</form>
	{/if}
</div>
