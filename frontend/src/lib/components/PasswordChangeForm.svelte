<script>
	/**
	 * Password change (F14, K11): used for the forced first-login change and on
	 * the account page. Password managers recognise the new-password field.
	 */
	import { createEventDispatcher } from 'svelte';
	import { ApiError, changePassword } from '#lib/api.js';

	/** Username, so password managers update the right entry. */
	export let username = '';
	export let submitLabel = 'Change password';

	const MIN_LENGTH = 12;
	const dispatch = createEventDispatcher();

	let currentPassword = '';
	let newPassword = '';
	let confirmation = '';
	let error = '';
	let submitting = false;

	async function submit() {
		error = '';
		if (newPassword.length < MIN_LENGTH) {
			error = `The new password must have at least ${MIN_LENGTH} characters.`;
			return;
		}
		if (newPassword !== confirmation) {
			error = 'The two new passwords differ.';
			return;
		}
		submitting = true;
		try {
			const result = await changePassword({ currentPassword, newPassword });
			currentPassword = '';
			newPassword = '';
			confirmation = '';
			dispatch('changed', result);
		} catch (err) {
			if (err instanceof ApiError && err.status === 403 && err.code !== 'CSRF_REJECTED') {
				error = 'The current password is incorrect.';
			} else {
				error = err?.message || 'The password could not be changed.';
			}
		} finally {
			submitting = false;
		}
	}
</script>

<form class="space-y-4" method="post" aria-label="Change password" on:submit|preventDefault={submit}>
	<!-- Hidden username field: lets password managers attach the new password to this account. -->
	<input type="text" name="username" value={username} autocomplete="username" readonly hidden />
	<div class="space-y-1">
		<label for="current-password" class="block text-sm font-medium text-dark-300">Current password</label>
		<input
			id="current-password"
			name="current-password"
			type="password"
			bind:value={currentPassword}
			autocomplete="current-password"
			required
			class="w-full rounded-lg border border-dark-700 bg-dark-800 px-3 py-2 text-dark-100 outline-hidden focus:border-green-500"
		/>
	</div>
	<div class="space-y-1">
		<label for="new-password" class="block text-sm font-medium text-dark-300">New password</label>
		<input
			id="new-password"
			name="new-password"
			type="password"
			bind:value={newPassword}
			autocomplete="new-password"
			minlength={MIN_LENGTH}
			required
			class="w-full rounded-lg border border-dark-700 bg-dark-800 px-3 py-2 text-dark-100 outline-hidden focus:border-green-500"
		/>
		<p class="text-xs text-dark-500">At least {MIN_LENGTH} characters.</p>
	</div>
	<div class="space-y-1">
		<label for="confirm-password" class="block text-sm font-medium text-dark-300">Confirm new password</label>
		<input
			id="confirm-password"
			name="confirm-password"
			type="password"
			bind:value={confirmation}
			autocomplete="new-password"
			required
			class="w-full rounded-lg border border-dark-700 bg-dark-800 px-3 py-2 text-dark-100 outline-hidden focus:border-green-500"
		/>
	</div>
	{#if error}
		<p class="text-sm text-red-400" role="alert">{error}</p>
	{/if}
	<button
		type="submit"
		disabled={submitting}
		class="rounded-lg bg-green-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-green-500 disabled:cursor-wait disabled:opacity-60"
	>
		{submitting ? 'Saving…' : submitLabel}
	</button>
</form>
