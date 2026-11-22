/**
 * Footprint sections (K7) and the old routes that redirect to them.
 */
import { redirect } from '@sveltejs/kit';

export const FOOTPRINT_SECTIONS = [
	{ href: '/footprint', label: 'Charts' },
	{ href: '/footprint/nodes', label: 'Nodes' },
	{ href: '/footprint/report', label: 'Report' }
];

/** Routes moved under `/footprint`: old path → new path. */
export const FOOTPRINT_REDIRECTS = {
	'/report': '/footprint/report',
	'/nodes': '/footprint/nodes'
};

/** `/footprint` is active only on itself; a sub-section on itself and below. */
export function isFootprintSectionActive(href, pathname = '') {
	const path = String(pathname).replace(/\/+$/, '') || '/';
	if (href === '/footprint') return path === '/footprint';
	return path === href || path.startsWith(`${href}/`);
}

/**
 * Permanently redirect an old route to its `/footprint` page, keeping the
 * query string (report filters) and the fragment-less path.
 * @param {string} from old path, a key of `FOOTPRINT_REDIRECTS`
 * @param {URL} [url] the requested URL
 */
export function redirectToFootprint(from, url) {
	const target = FOOTPRINT_REDIRECTS[from];
	if (!target) throw new Error(`No footprint redirect for ${from}`);
	redirect(308, `${target}${url?.search ?? ''}`);
}
