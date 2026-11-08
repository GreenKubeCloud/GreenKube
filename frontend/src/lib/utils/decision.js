/**
 * Presentation helpers for the server's decision on a recommendation:
 * the risk-adjusted expected value (`value`, roadmap I1) and the delivery
 * eligibility (`eligibility`, roadmap F10). The server computes both; the UI
 * only formats them and never re-derives eligibility from the action itself.
 */

function finite(value) {
	const number = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
	return typeof number === 'number' && Number.isFinite(number) ? number : null;
}

/** Expected value per year, or null when the server returned none. */
export function expectedValue(rec) {
	return finite(rec?.value?.ev);
}

/** Probability of success (0–1), or null. */
export function successProbability(rec) {
	return finite(rec?.value?.p_success);
}

/** Currency amount with no decimals above 10, e.g. "€412" or "-$3.50". */
export function formatMoney(amount, currency = 'USD') {
	const value = finite(amount);
	if (value === null) return '—';
	const digits = Math.abs(value) >= 10 ? 0 : 2;
	try {
		return new Intl.NumberFormat('en-US', {
			style: 'currency',
			currency: typeof currency === 'string' && /^[A-Z]{3}$/.test(currency) ? currency : 'USD',
			minimumFractionDigits: digits,
			maximumFractionDigits: digits
		}).format(value);
	} catch {
		return `${value.toFixed(digits)} ${currency}`;
	}
}

/** One-line summary, e.g. "≈ $412/yr expected · 82% success"; null without a value. */
export function describeExpectedValue(rec) {
	const ev = expectedValue(rec);
	if (ev === null) return null;
	const p = successProbability(rec);
	const money = formatMoney(ev, rec?.value?.currency);
	return p === null ? `≈ ${money}/yr expected` : `≈ ${money}/yr expected · ${Math.round(p * 100)}% success`;
}

/** True for reliability recommendations (the change costs money to reduce incident risk). */
export function isReliabilityLane(rec) {
	return rec?.value?.lane === 'reliability';
}

function entries(list) {
	return Array.isArray(list)
		? list
				.filter((item) => item && typeof item === 'object' && typeof item.code === 'string')
				.map((item) => ({
					code: item.code,
					message: typeof item.message === 'string' ? item.message : '',
					fix: typeof item.fix === 'string' ? item.fix : ''
				}))
		: [];
}

/** Normalized eligibility, or null when the server did not return one. */
export function eligibilityOf(rec) {
	const eligibility = rec?.eligibility;
	if (!eligibility || typeof eligibility !== 'object') return null;
	return {
		canOpenPr: eligibility.can_open_pr === true,
		deliveryModes: Array.isArray(eligibility.delivery_modes) ? eligibility.delivery_modes : [],
		blockers: entries(eligibility.blockers),
		warnings: entries(eligibility.warnings),
		eligibleAt: typeof eligibility.eligible_at === 'string' ? eligibility.eligible_at : null,
		recommendationIds: Array.isArray(eligibility.recommendation_ids) ? eligibility.recommendation_ids : null
	};
}

/**
 * The change set a plan is created for, when the server says a PR can be opened.
 * Replaces the former client-side `safeActionFor` checks.
 * @returns {{recommendationIds: number[]} | null}
 */
export function planTargetFor(rec) {
	const eligibility = eligibilityOf(rec);
	const id = Number(rec?.id);
	if (!eligibility?.canOpenPr || !Number.isInteger(id) || id <= 0) return null;
	const ids = (eligibility.recommendationIds ?? [id]).map(Number);
	if (!ids.length || !ids.every((item) => Number.isInteger(item) && item > 0) || !ids.includes(id)) return null;
	return { recommendationIds: [...new Set(ids)] };
}
