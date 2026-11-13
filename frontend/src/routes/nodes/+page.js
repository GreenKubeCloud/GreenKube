/** `/nodes` moved to `/footprint/nodes` (K7): redirect, keeping the query string. */
import { redirectToFootprint } from '../footprint/sections.js';

export function load({ url }) {
	redirectToFootprint('/nodes', url);
}
