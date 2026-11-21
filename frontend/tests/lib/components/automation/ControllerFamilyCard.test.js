/**
 * Tests for the GitOps controller family card.
 */
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/svelte';
import ControllerFamilyCard from '#lib/components/automation/ControllerFamilyCard.svelte';
import { groupControllerFamilies } from '#lib/components/automation/gitopsControllers.js';

const available = { available: true, reason: null };
const missing = { available: false, reason: 'crd-not-found' };

function families(overrides = {}) {
	return groupControllerFamilies({
		argocd: available,
		flux_kustomization: available,
		flux_helm_release: available,
		flux_source_git_repository: available,
		flux_source_oci_repository: available,
		flux_source_helm_repository: available,
		flux_source_bucket: available,
		...overrides
	});
}

describe('ControllerFamilyCard', () => {
	it('renders one labelled article with a heading, status, and grouped resource kinds', () => {
		const flux = families()[1];
		render(ControllerFamilyCard, { props: { family: flux } });

		const card = screen.getByRole('article', { name: 'Flux' });
		expect(within(card).getByRole('heading', { level: 3, name: 'Flux' })).toBeInTheDocument();
		expect(within(card).getByLabelText('Flux status: Detected')).toHaveTextContent('Detected');
		expect(within(card).getByText('All 6 resource kinds are available.')).toBeInTheDocument();

		const reconcilers = within(card).getByRole('list', { name: 'Reconcilers' });
		expect(within(reconcilers).getAllByRole('listitem')).toHaveLength(2);
		expect(within(reconcilers).getByText('Kustomizations')).toBeInTheDocument();
		expect(within(reconcilers).getByText('HelmReleases')).toBeInTheDocument();

		const sources = within(card).getByRole('list', { name: 'Sources' });
		expect(within(sources).getAllByRole('listitem')).toHaveLength(4);
		expect(within(sources).getByText('Buckets')).toBeInTheDocument();
	});

	it('explains a degraded family and shows per-kind status as text, not color only', () => {
		const flux = families({ flux_helm_release: missing, flux_source_bucket: missing })[1];
		const { container } = render(ControllerFamilyCard, { props: { family: flux } });

		expect(screen.getByLabelText('Flux status: Degraded')).toBeInTheDocument();
		expect(screen.getByText(/4 of 6 resource kinds available/)).toBeInTheDocument();
		expect(screen.getByText('Unavailable: HelmReleases, Buckets.')).toBeInTheDocument();
		const helmRelease = container.querySelector('[data-kind="flux_helm_release"]');
		expect(helmRelease).toHaveTextContent('Not installed · CRD not detected');
		expect(container.querySelector('[data-kind="flux_kustomization"]')).toHaveTextContent('Available');
	});

	it('renders the single-kind Argo CD family with the same structure', () => {
		const argo = families({ argocd: missing })[0];
		render(ControllerFamilyCard, { props: { family: argo } });

		const card = screen.getByRole('article', { name: 'Argo CD' });
		expect(within(card).getByLabelText('Argo CD status: Not detected')).toBeInTheDocument();
		expect(
			within(card).getByText('No Argo CD custom resource definitions were found in this cluster.')
		).toBeInTheDocument();
		expect(within(card).getByRole('list', { name: 'Reconcilers' })).toHaveTextContent('Applications');
	});

	it('shows an unknown status when discovery data is missing', () => {
		const argo = groupControllerFamilies(null)[0];
		render(ControllerFamilyCard, { props: { family: argo } });

		expect(screen.getByLabelText('Argo CD status: Unknown')).toBeInTheDocument();
		expect(
			screen.getByText('Availability could not be determined from the latest discovery status.')
		).toBeInTheDocument();
	});
});
