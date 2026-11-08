<script>
	/**
	 * Sign-in form (F14, K11). A real <form> with username/current-password
	 * autocomplete and a native submit, so browsers and password managers offer
	 * to save and autofill the credentials.
	 */
	import { createEventDispatcher } from 'svelte';
	import { ApiError, login } from '$lib/api.js';

	const dispatch = createEventDispatcher();

	let username = '';
	let password = '';
	let remember = false;
	let error = '';
	let submitting = false;

	function messageFor(err) {
		if (err instanceof ApiError) {
			if (err.status === 429) return 'Too many attempts. Wait a minute and try again.';
			if (err.status === 401) return 'Invalid username or password.';
			if (err.status === 409) return 'Browser sign-in is disabled on this instance; use an API key.';
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
		class="w-full rounded-lg bg-green-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-green-500 disabled:cursor-wait disabled:opacity-60"
	>
		{submitting ? 'Signing in…' : 'Sign in'}
	</button>
</form>
