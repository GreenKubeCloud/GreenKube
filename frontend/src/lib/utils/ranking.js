/**
 * Presentation helpers for recommendation ranking (K2).
 *
 * The server attaches `ranking_score` and a `ranking_factors` breakdown to
 * each recommendation. Since I1 the score is the risk-adjusted expected value
 * and the factors are its terms in currency: positive gains (`cost`,
 * `carbon`, `reliability`) and negative costs (`incident_risk`, `effort`).
 * Every helper tolerates numbers and `{value}`-style entries.
 */

const FACTOR_LABELS = {
	carbon: 'Carbon value',
	co2: 'Carbon savings',
	cost: 'Cost savings',
	value: 'Expected value',
	confidence: 'Confidence',
	risk: 'Low risk',
	reliability: 'Incidents avoided',
	incident_risk: 'Incident risk',
	effort: 'Effort',
	actionability: 'Ready as a change',
	source: 'Source authority',
	track_record: 'Source track record',
	p_success: 'Success probability'
};

/** Human label for a ranking factor key. */
export function rankingFactorLabel(key) {
	if (typeof key !== 'string' || !key) return 'Unknown factor';
	if (FACTOR_LABELS[key]) return FACTOR_LABELS[key];
	const words = key.replace(/[_-]+/g, ' ').trim();
	return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Unknown factor';
}

/** Numeric contribution of one factor entry, or null when it has none. */
export function rankingFactorValue(entry) {
	const raw =
		entry !== null && typeof entry === 'object'
			? entry.value ?? entry.contribution ?? entry.score ?? entry.amount
			: entry;
	const value = typeof raw === 'string' && raw.trim() !== '' ? Number(raw) : raw;
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Factors sorted by contribution, largest first; entries without a number are skipped. */
export function sortedRankingFactors(factors) {
	if (!factors || typeof factors !== 'object' || Array.isArray(factors)) return [];
	return Object.entries(factors)
		.map(([key, entry]) => ({ key, label: rankingFactorLabel(key), value: rankingFactorValue(entry) }))
		.filter((factor) => factor.value !== null)
		.sort((a, b) => b.value - a.value || a.key.localeCompare(b.key));
}

/** The factor contributing most to the score, or null when none contributes. */
export function topRankingFactor(factors) {
	const [top] = sortedRankingFactors(factors);
	return top && top.value > 0 ? top : null;
}

/** Score with two decimals, or an em dash when it is missing. */
export function formatRankingScore(score) {
	const value = typeof score === 'string' && score.trim() !== '' ? Number(score) : score;
	return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '—';
}

/** One-line breakdown for a tooltip, e.g. "Cost savings 0.30 · Confidence 0.12". */
export function describeRankingFactors(factors) {
	return sortedRankingFactors(factors)
		.map((factor) => `${factor.label} ${factor.value.toFixed(2)}`)
		.join(' · ');
}
