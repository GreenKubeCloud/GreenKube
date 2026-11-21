<script>
	/**
	 * Sign-in form (F14, F16, K11). A real <form> with username/current-password
	 * autocomplete and a native submit, so browsers and password managers offer
	 * to save and autofill the credentials. With single sign-on configured it
	 * also offers the SSO button (OpenID Connect) or explains that the
	 * authentication proxy signs users in, and shows why an SSO attempt failed.
	 * With API_AUTH_MODE=api_key it asks for an API key instead (F-09): the key
	 * is sent once and kept by the server in an HttpOnly cookie, never here.
	 */
	import { createEventDispatcher, onMount } from 'svelte';
	import { ApiError, getAuthProviders, login, loginWithApiKey, ssoLoginUrl } from '$lib/api.js';

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
	let apiKey = '';
	let remember = false;
	let error = '';
	let submitting = false;
	let providers = null;
	let providersLoading = true;
	let providersError = '';
	let ssoError = '';

	$: apiKeyEnabled = Boolean(providers && (providers.api_key === true || providers.mode === 'api_key'));
	// Without the sign-in options, no form is shown: guessing one would answer 409 in api_key or SSO-only modes.
	$: passwordEnabled = Boolean(providers) && !apiKeyEnabled && providers.password !== false;
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
		await loadProviders();
	});

	async function loadProviders() {
		providersLoading = true;
		providersError = '';
		providers = await getAuthProviders();
		if (!providers) providersError = 'GreenKube could not load the sign-in options. Check that it is reachable and try again.';
		providersLoading = false;
	}

	function messageFor(err) {
		if (err instanceof ApiError) {
			if (err.status === 429) return 'Too many attempts. Wait a minute and try again.';
			if (err.status === 401) return 'Invalid username or password.';
			if (err.status === 409) return err.message || 'Password sign-in is disabled on this instance.';
			if (err.status === 503) return 'The account store is not available yet. Try again shortly.';
		}
		return err?.message || 'Sign-in failed.';
	}

	function apiKeyMessageFor(err) {
		if (err instanceof ApiError) {
			if (err.status === 401) return 'Invalid API key.';
			if (err.status === 429) return 'Too many attempts. Wait a minute and try again.';
			if (err.status === 409) return err.message || 'API-key sign-in is disabled on this instance.';
		}
		return err?.message || 'Sign-in failed.';
	}

	async function submitApiKey() {
		const key = apiKey.trim();
		if (!key) {
			error = 'Enter your API key.';
			return;
		}
		submitting = true;
		error = '';
		try {
			const principal = await loginWithApiKey({ key, remember });
			apiKey = '';
			dispatch('signedin', principal);
		} catch (err) {
			error = apiKeyMessageFor(err);
		} finally {
			submitting = false;
		}
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

	{#if providersLoading}
		<p class="text-sm text-dark-500" role="status">Loading sign-in options…</p>
	{:else if providersError}
		<div class="space-y-3" data-testid="providers-error">
			<p class="rounded-lg border border-red-800/60 bg-red-900/20 px-3 py-2 text-sm text-red-300" role="alert">
				{providersError}
			</p>
			<button
				type="button"
				on:click={loadProviders}
				class="w-full rounded-lg border border-dark-600 bg-dark-800 px-4 py-2 text-sm font-medium text-dark-100 hover:bg-dark-700"
			>
				Try again
			</button>
		</div>
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

	{#if apiKeyEnabled}
		<form
			class="space-y-4"
			method="post"
			action="/api/v1/auth/api-key-login"
			aria-label="Sign in to GreenKube with an API key"
			on:submit|preventDefault={submitApiKey}
		>
			<div class="space-y-1">
				<label for="login-api-key" class="block text-sm font-medium text-dark-300">API key</label>
				<input
					id="login-api-key"
					name="key"
					type="password"
					bind:value={apiKey}
					autocomplete="off"
					autocapitalize="none"
					spellcheck="false"
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
				class="w-full rounded-lg bg-green-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-green-500 disabled:cursor-wait disabled:opacity-60"
			>
				{submitting ? 'Signing in…' : 'Sign in'}
			</button>
		</form>
		<p class="text-xs text-dark-500" data-testid="api-key-hint">
			This instance uses <code>apiAuthMode: api_key</code>: sign in with the bootstrap key
			(<code>GREENKUBE_API_KEY</code>) or a scoped <code>gk_</code> key. For user accounts, switch to
			<code>apiAuthMode: session</code>.
		</p>
	{:else if passwordEnabled}
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
		<p class="text-xs text-dark-500">
			First sign-in? Use the initial admin password printed by <code>helm install</code>. Scripts and
			Prometheus use API keys instead.
		</p>
	{/if}
</div>
