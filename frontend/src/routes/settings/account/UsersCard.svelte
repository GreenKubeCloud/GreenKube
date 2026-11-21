<script>
	/**
	 * Users and roles (F16, admins only): local accounts and SSO accounts with
	 * their role. Local accounts get a role here; SSO accounts get theirs from
	 * the group mapping at every sign-in. The API refuses to demote or disable
	 * the last active admin.
	 */
	import { onMount } from 'svelte';
	import { createUser, listUsers, updateUser } from '#lib/api.js';
	import Card from '#lib/components/Card.svelte';
	import DataState from '#lib/components/DataState.svelte';

	export let currentUserName = '';

	const ROLES = ['viewer', 'operator', 'admin'];
	const SOURCES = { local: 'Local', proxy: 'SSO (proxy)', oidc: 'SSO (OIDC)' };

	let users = [];
	let loading = true;
	let error = null;
	let actionError = '';
	let notice = '';
	let newUsername = '';
	let newPassword = '';
	let newRole = 'viewer';
	let creating = false;

	async function load() {
		loading = true;
		error = null;
		try {
			users = await listUsers();
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	function replace(updated) {
		users = users.map((user) => (user.id === updated.id ? updated : user));
	}

	async function changeRole(user, role) {
		actionError = '';
		notice = '';
		if (role === user.role) return;
		try {
			replace(await updateUser(user.id, { role }));
			notice = `${user.username} is now ${role}.`;
		} catch (e) {
			actionError = e.message;
			users = [...users];
		}
	}

	async function toggleDisabled(user) {
		actionError = '';
		notice = '';
		try {
			replace(await updateUser(user.id, { disabled: !user.disabled }));
			notice = user.disabled ? `${user.username} can sign in again.` : `${user.username} is disabled and signed out.`;
		} catch (e) {
			actionError = e.message;
		}
	}

	async function submit() {
		actionError = '';
		notice = '';
		if (!newUsername.trim() || newPassword.length < 12) {
			actionError = 'Enter a username and an initial password of at least 12 characters.';
			return;
		}
		creating = true;
		try {
			const created = await createUser({ username: newUsername.trim(), password: newPassword, role: newRole });
			users = [...users, created].sort((a, b) => a.username.localeCompare(b.username));
			notice = `${created.username} created: they choose a new password at their first sign-in.`;
			newUsername = '';
			newPassword = '';
			newRole = 'viewer';
		} catch (e) {
			actionError = e.message;
		} finally {
			creating = false;
		}
	}

	onMount(load);
</script>

<Card title="Users and roles" icon="👥">
	<p class="mb-3 text-xs text-dark-500">
		Viewer: read. Operator: approve plans, ignore and restore, mark applied. Admin: users, API keys, repository
		allowlist. Single sign-on accounts get their role from their groups at each sign-in.
	</p>
	<DataState {loading} {error} empty={users.length === 0}>
		<table class="w-full text-sm" aria-label="Users">
			<thead>
				<tr class="text-left text-xs text-dark-500">
					<th class="py-2 font-medium">User</th>
					<th class="py-2 font-medium">Sign-in</th>
					<th class="py-2 font-medium">Role</th>
					<th class="py-2 font-medium"><span class="sr-only">Actions</span></th>
				</tr>
			</thead>
			<tbody class="divide-y divide-dark-800">
				{#each users as user (user.id)}
					<tr class:opacity-60={user.disabled}>
						<td class="py-2 text-dark-200">
							{user.username}
							{#if user.username === currentUserName}<span class="badge-green ml-2">You</span>{/if}
							{#if user.disabled}<span class="ml-2 text-[10px] text-red-400">disabled</span>{/if}
						</td>
						<td class="py-2 text-dark-400">{SOURCES[user.auth_source] ?? user.auth_source}</td>
						<td class="py-2">
							{#if user.role_managed_by_groups}
								<span class="text-dark-300" title="Set by the identity provider groups">{user.role}</span>
							{:else}
								<select
									class="rounded border border-dark-700 bg-dark-800 px-2 py-1 text-dark-100"
									aria-label={`Role of ${user.username}`}
									value={user.role}
									on:change={(event) => changeRole(user, event.currentTarget.value)}
								>
									{#each ROLES as role}
										<option value={role}>{role}</option>
									{/each}
								</select>
							{/if}
						</td>
						<td class="py-2 text-right">
							<button
								type="button"
								class="btn-secondary text-xs !py-1 !px-2"
								on:click={() => toggleDisabled(user)}
								aria-label={`${user.disabled ? 'Enable' : 'Disable'} ${user.username}`}
							>
								{user.disabled ? 'Enable' : 'Disable'}
							</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</DataState>

	<form class="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-4" aria-label="Create a user" on:submit|preventDefault={submit}>
		<label class="sr-only" for="new-user-name">Username</label>
		<input
			id="new-user-name"
			name="new-username"
			bind:value={newUsername}
			autocomplete="off"
			placeholder="username"
			class="rounded-lg border border-dark-700 bg-dark-800 px-3 py-1.5 text-dark-100"
		/>
		<label class="sr-only" for="new-user-password">Initial password</label>
		<input
			id="new-user-password"
			name="new-password"
			type="password"
			bind:value={newPassword}
			autocomplete="new-password"
			placeholder="initial password"
			class="rounded-lg border border-dark-700 bg-dark-800 px-3 py-1.5 text-dark-100"
		/>
		<label class="sr-only" for="new-user-role">Role</label>
		<select id="new-user-role" bind:value={newRole} class="rounded-lg border border-dark-700 bg-dark-800 px-2 py-1.5 text-dark-100">
			{#each ROLES as role}
				<option value={role}>{role}</option>
			{/each}
		</select>
		<button type="submit" class="btn-secondary text-xs" disabled={creating}>{creating ? 'Creating…' : 'Create user'}</button>
	</form>
	{#if notice}
		<p class="mt-2 text-xs text-green-400" role="status">{notice}</p>
	{/if}
	{#if actionError}
		<p class="mt-2 text-xs text-red-400" role="alert">{actionError}</p>
	{/if}
</Card>
