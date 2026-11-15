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
		userDeclaredAnnual: firstBlock(savings?.user_declared_annual)
	};
}

/** "Realization factor 45%: about this share of an allocated saving reaches the bill." or ''. */
export function realizationText(factor) {
	const value = finite(factor);
	if (value === null) return '';
	return `${SAVINGS_TERMS.realizationFactor} ${Math.round(value * 100)}%: the expected share of an allocated saving that reaches the bill once capacity is removed.`;
}
