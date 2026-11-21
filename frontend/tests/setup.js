import '@testing-library/jest-dom/vitest';
import { beforeEach } from 'vitest';
import { currentUser } from '#lib/stores.js';

/**
 * Pages render only for a signed-in principal (the layout waits for /auth/me),
 * and their mutating actions are gated on its scopes. Tests run as an operator
 * unless they set another principal (viewer tests, the sign-in flow).
 */
export const TEST_OPERATOR = {
	authenticated: true,
	kind: 'user',
	name: 'operator',
	actor: 'user:operator',
	role: 'operator',
	scopes: ['read', 'write', 'approve', 'plans:deliver', 'metrics:read'],
	must_change_password: false
};

beforeEach(() => {
	currentUser.set(TEST_OPERATOR);
});
