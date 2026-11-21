/**
 * One savings vocabulary for every page (roadmap principle 3:
 * projected ≠ allocated ≠ realized).
 *
 * - **Projected**: GreenKube's estimate before a change is measured, on the
 *   allocated basis like a measurement (ledger `basis: allocated`). The
 *   billable expectation (allocated x realization factor) is a derived figure
 *   (`realization.projected_billable_cost_annual_expected`), never accrued.
 * - **Measured allocated**: what the changed workloads stop being charged,
 *   measured after the change (ledger method `measured_allocated`). The
 *   cluster bill drops only when capacity is actually removed.
 * - **Realized (billed)**: a saving seen on the bill (ledger method
 *   `realized_billed`). The word "Realized" is reserved for it.
 *
 * The savings report is read defensively: explicit fields
 * (`measured_allocated_*`, `realized_billed_*`, `realization_factor`) are used
 * when the server sends them, otherwise the existing ones
 * (`realized_to_date` and `run_rate_annual`, which are measured allocated
 * figures, and `realization.realization_factor`).
 */

export const SAVINGS_TERMS = {
	measuredAllocated: 'Measured allocated savings',
	measuredAllocatedToDate: 'Measured allocated to date',
	measuredAllocatedRunRate: 'Measured allocated run rate',
	allocatedCo2e: 'Allocated CO₂e',
	realizedBilled: 'Realized (billed) savings',
	realizationFactor: 'Realization factor',
	pendingVerification: 'Pending verification',
	userDeclared: 'User declared'
};

/** Outcome of one measurement (`outcome`, change `state`), never "Realized" for an allocated figure. */
export const OUTCOME_LABELS = {
	realized: 'Saving measured',
	not_realized: 'No saving measured',
	inconclusive: 'Inconclusive',
	contaminated: 'Contaminated'
};

export function outcomeLabel(outcome) {
	if (!outcome) return '—';
	return OUTCOME_LABELS[outcome] ?? String(outcome).replaceAll('_', ' ');
}

function finite(value) {
	const number = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
	return typeof number === 'number' && Number.isFinite(number) ? number : null;
}

function isBlock(value) {
	return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function firstBlock(...candidates) {
	return candidates.find(isBlock) ?? null;
}

/** Realization factor in [0, 1] from the explicit field or `realization.realization_factor`. */
export function realizationFactorOf(savings) {
	const value = finite(savings?.realization_factor ?? savings?.realization?.realization_factor);
	if (value === null) return null;
	return Math.min(Math.max(value, 0), 1);
}

/**
 * The savings report's figures under their honest names. Blocks keep the
 * server shape (`cost`, `co2e_grams`, `method`); missing ones are null.
 */
export function savingsFigures(savings) {
	const realization = isBlock(savings?.realization) ? savings.realization : {};
	return {
		measuredAllocatedToDate: firstBlock(savings?.measured_allocated_to_date, savings?.realized_to_date),
		measuredAllocatedAnnual: firstBlock(savings?.measured_allocated_annual, savings?.run_rate_annual),
		// Only a billed measurement is "realized": never derived from an allocated figure.
		realizedBilledToDate: firstBlock(savings?.realized_billed_to_date),
		realizedBilledAnnual: firstBlock(savings?.realized_billed_annual),
		realizationFactor: realizationFactorOf(savings),
		expectedBilledCostAnnual: finite(realization.realized_billed_cost_annual_expected),
		expectedBilledCo2eAnnual: finite(realization.realized_billed_co2e_grams_annual_expected),
		projectedAnnual: firstBlock(savings?.projected_annual),
		projectedToDate: firstBlock(savings?.projected_to_date),
		userDeclaredAnnual: firstBlock(savings?.user_declared_annual),
		// Measured cost of reliability fixes (negative ledger entries, MEA-14): a cost, never netted into a saving.
		reliabilityCostToDate: firstBlock(savings?.reliability_cost_to_date)
	};
}

function raisesRequest(rec) {
	if (rec?.value?.lane === 'reliability') return true;
	const pairs = {
		RIGHTSIZING_CPU: ['current_cpu_request_millicores', 'recommended_cpu_request_millicores'],
		RIGHTSIZING_MEMORY: ['current_memory_request_bytes', 'recommended_memory_request_bytes']
	}[rec?.type];
	if (!pairs) return false;
	const current = finite(rec?.[pairs[0]]);
	const recommended = finite(rec?.[pairs[1]]);
	return current !== null && recommended !== null && recommended > current;
}

/**
 * The cost figure of an applied recommendation's card. A reliability fix (a
 * request increase, MEA-14) is judged on its OOM-kill rate, not on a saving:
 * once verified it reads "Fix confirmed, costs" with what the fix costs per
 * year (positive), never as a measured saving of 0. Other records keep "Cost
 * Measured" once verified, else "Cost Projected".
 * @returns {{label: string, value: number|null, reliability: boolean}}
 */
export function appliedCostFigure(rec) {
	const verified = rec?.status === 'verified';
	if (raisesRequest(rec)) {
		const projected = finite(rec?.cost_saved);
		const cost = projected === null ? null : -projected;
		return { label: verified ? 'Fix confirmed, costs' : 'Fix projected to cost', value: cost, reliability: true };
	}
	const measured = verified ? finite(rec?.measured_cost_saved) : null;
	return measured !== null
		? { label: 'Cost Measured', value: measured, reliability: false }
		: { label: 'Cost Projected', value: finite(rec?.cost_saved), reliability: false };
}

/** "Realization factor 45%: about this share of an allocated saving reaches the bill." or ''. */
export function realizationText(factor) {
	const value = finite(factor);
	if (value === null) return '';
	return `${SAVINGS_TERMS.realizationFactor} ${Math.round(value * 100)}%: the expected share of an allocated saving that reaches the bill once capacity is removed.`;
}

/**
 * A saving as a positive phrase: `saves $232.53/yr`. A negative value is an
 * increase and keeps its meaning: `costs $40.00 more/yr`. `verb` is `saves`
 * for projections and `saved` for applied results; `suffix` follows the
 * amount (for example ` CO₂e/yr`). Null for a missing or zero value.
 */
export function savingPhrase(value, format, { verb = 'saves', suffix = '/yr' } = {}) {
	const number = Number(value);
	if (value === null || value === undefined || !Number.isFinite(number) || number === 0) return null;
	if (number > 0) return `${verb} ${format(number)}${suffix}`;
	return `${verb === 'saved' ? 'cost' : 'costs'} ${format(-number)} more${suffix}`;
}

/**
 * Per-recommendation diagnostics that only say "this install has no GitOps
 * automation". In demo mode the page shows one demo notice instead; on a real
 * install they are always shown.
 */
export const AUTOMATION_UNAVAILABLE_CODES = new Set([
	'action_unavailable',
	'automation_not_configured',
	'automation_disabled'
]);

/** The eligibility blockers and warnings a card shows (demo mode drops the automation-unavailable ones). */
export function visibleDiagnostics(eligibility, demo = false) {
	const keep = (item) => !(demo && AUTOMATION_UNAVAILABLE_CODES.has(item?.code));
	return {
		blockers: (eligibility?.blockers ?? []).filter(keep),
		warnings: (eligibility?.warnings ?? []).filter(keep)
	};
}
