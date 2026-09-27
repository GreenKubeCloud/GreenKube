# src/greenkube/automation/manifests/__init__.py
"""Manifest patchers used by the automation service."""

from greenkube.automation.manifests.patcher import ManifestNotFoundError, PatchResult, RightsizingPatcher

__all__ = ["ManifestNotFoundError", "PatchResult", "RightsizingPatcher"]
