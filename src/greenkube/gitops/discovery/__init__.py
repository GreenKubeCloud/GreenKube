"""Pure discovery adapters for Argo CD and Flux resources."""

from greenkube.gitops.discovery.argo import discover_argo_application
from greenkube.gitops.discovery.flux import (
    discover_flux_helm_release,
    discover_flux_kustomization,
    parse_flux_inventory_id,
)

__all__ = [
    "discover_argo_application",
    "discover_flux_helm_release",
    "discover_flux_kustomization",
    "parse_flux_inventory_id",
]
