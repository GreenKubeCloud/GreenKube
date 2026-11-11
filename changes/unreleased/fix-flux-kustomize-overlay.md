### Fixed
- **Flux Kustomizations with `targetNamespace` (e2e defect, roadmap O1/D):** plans for workloads deployed by a Flux Kustomization whose `spec.targetNamespace` differs from the overlay or manifest namespace were rejected. The Kustomize planner now renders the overlay the way Flux builds it (`targetNamespace`, `namePrefix` and `nameSuffix` written into the root Kustomization, for the render only) and matches the live workload against that build. The managed patch selector names the workload as Kustomize sees it when the overlay's patches run, before its `namespace`, `namePrefix` and `nameSuffix` transformers (a namespace-less manifest is selected as `default`); it is found with one extra render and must be unique.
- **Kustomize plans fail closed on Flux build changes they cannot reproduce:** Flux discovery now records `namePrefix`, `nameSuffix`, `postBuild`, `patches`, `images` and `components`. A plan is rejected when Flux and the overlay both set a name prefix or suffix, when Flux `patches`/`images`/`components` would change the build after GreenKube's patch, or when `postBuild` substitution variables appear in the target's name, namespace, container names or container resources.
- **Kustomize overlays without a namespace under Argo CD:** a namespace-less workload rendered by an overlay matches its Argo CD destination namespace, as for raw manifests (a Flux workload needs `targetNamespace`).
- **Kustomize rollback of Flux overlays (D14):** rollback previews render the overlay with the same Flux build overrides, so the exact-target validation sees the live identity.

### Changed
- The quickstart Flux overlay (`docs/examples/quickstart/gitops-repo/flux/oversized-worker`) no longer declares a namespace: the Flux Kustomization's `targetNamespace` places it, so `make e2e` exercises that path.
- The e2e harness (`hack/e2e/loop.sh`) and the first-PR guide use the recommendations' `eligibility` field and the plan API (`/automation/plans/{id}`, `operation_status`) instead of the deprecated `/recommendations/{id}/apply-pr/eligibility` and `/automation/operations/{id}` routes.

### Removed
- The uncalled `validate_service_url` helper, the `ServiceConfigUpdate` request model of the removed `POST /config/services` route, and the `SERVICE_URL_ALLOWLIST` setting (Helm `config.serviceUrlAllowlist`) that only that helper read.
