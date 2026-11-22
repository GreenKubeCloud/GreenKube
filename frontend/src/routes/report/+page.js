/** `/report` moved to `/footprint/report` (K7): redirect, keeping the query string. */
import { redirectToFootprint } from '../footprint/sections.js';

export function load({ url }) {
	redirectToFootprint('/report', url);
}
