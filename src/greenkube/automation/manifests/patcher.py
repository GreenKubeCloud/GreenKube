# src/greenkube/automation/manifests/patcher.py
"""YAML manifest patching for rightsizing recommendations.

Uses ``ruamel.yaml`` round-trip mode so comments and formatting survive the
edit. The patcher locates the document matching the recommendation's owner kind
and name and rewrites container resource requests.
"""

from __future__ import annotations

import difflib
import io
import logging
from dataclasses import dataclass
from typing import List, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from greenkube.models.metrics import RecommendationRecord

logger = logging.getLogger(__name__)

_WORKLOAD_PATHS = {
    "Deployment": ("spec", "template", "spec", "containers"),
    "StatefulSet": ("spec", "template", "spec", "containers"),
    "DaemonSet": ("spec", "template", "spec", "containers"),
    "ReplicaSet": ("spec", "template", "spec", "containers"),
    "Job": ("spec", "template", "spec", "containers"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec", "containers"),
}

RESOURCE_FIELDS = {
    "cpu": "recommended_cpu_request_millicores",
    "memory": "recommended_memory_request_bytes",
}


class ManifestNotFoundError(LookupError):
    """Raised when the workload manifest cannot be located in the repository."""


@dataclass
class PatchResult:
    """Result of patching a manifest file."""

    path: str
    original: str
    patched: str
    diff: str

    @property
    def changed(self) -> bool:
        return self.original != self.patched


def _new_yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _humanize_bytes(value: int) -> str:
    for suffix, factor in (("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if value >= factor and value % factor == 0:
            return f"{value // factor}{suffix}"
    return str(value)


def _containers_for(doc: CommentedMap) -> Optional[List]:
    kind = doc.get("kind")
    path = _WORKLOAD_PATHS.get(kind)
    if path is None:
        return None
    node = doc
    for key in path:
        if not isinstance(node, CommentedMap) or key not in node:
            return None
        node = node[key]
    if not isinstance(node, list):
        return None
    return node


def _operation_values(record: RecommendationRecord) -> dict:
    """Builds the target values from the machine-readable patch or the DTO."""
    values: dict = {}
    patch = record.patch or {}
    for operation in patch.get("operations", []) or []:
        if operation.get("op") != "set_container_resources":
            continue
        resource = operation.get("resource")
        value = operation.get("value")
        if resource and value is not None:
            values.setdefault(resource, (operation.get("container"), value))

    if not values:
        if record.recommended_cpu_request_millicores is not None:
            values["cpu"] = (None, f"{record.recommended_cpu_request_millicores}m")
        if record.recommended_memory_request_bytes is not None:
            values["memory"] = (None, _humanize_bytes(record.recommended_memory_request_bytes))
    return values


class RightsizingPatcher:
    """Applies CPU/memory request edits to workload manifests."""

    def patch_content(self, record: RecommendationRecord, content: str, path: str = "") -> PatchResult:
        """Returns the patched content and unified diff for one manifest file.

        Raises:
            ManifestNotFoundError: When no document matches the record's owner.
        """
        yaml = _new_yaml()
        try:
            documents = list(yaml.load_all(content))
        except Exception as exc:
            raise ManifestNotFoundError(f"Could not parse YAML from '{path}': {exc}") from exc

        target_index = self._find_document(documents, record)
        if target_index is None:
            raise ManifestNotFoundError(f"No {record.owner_kind} named '{record.owner_name}' found in '{path}'.")

        values = _operation_values(record)
        if not values:
            raise ManifestNotFoundError(f"Recommendation {record.id} has no CPU/memory target to apply.")

        doc = documents[target_index]
        containers = _containers_for(doc)
        if containers is None:
            raise ManifestNotFoundError(f"Could not locate containers in {record.owner_kind} '{record.owner_name}'.")

        self._apply_to_containers(containers, values)

        buffer = io.StringIO()
        for index, document in enumerate(documents):
            if index > 0:
                buffer.write("---\n")
            yaml.dump(document, buffer)

        patched = buffer.getvalue()
        diff = "\n".join(
            difflib.unified_diff(
                content.splitlines(),
                patched.splitlines(),
                fromfile=f"a/{path}" if path else "a/manifest.yaml",
                tofile=f"b/{path}" if path else "b/manifest.yaml",
                lineterm="",
            )
        )
        return PatchResult(path=path, original=content, patched=patched, diff=diff)

    def find_path(self, record: RecommendationRecord, files: dict) -> Optional[str]:
        """Finds the manifest path whose content matches the record's owner.

        ``files`` maps path → content. Returns None when no match is found.
        """
        for path, content in files.items():
            try:
                yaml = _new_yaml()
                documents = list(yaml.load_all(content))
            except Exception:
                continue
            if self._find_document(documents, record) is not None:
                return path
        return None

    def _find_document(self, documents: List, record: RecommendationRecord) -> Optional[int]:
        for index, doc in enumerate(documents):
            if not isinstance(doc, CommentedMap):
                continue
            if doc.get("kind") != record.owner_kind:
                continue
            metadata = doc.get("metadata") or {}
            if metadata.get("name") != record.owner_name:
                continue
            if record.namespace and metadata.get("namespace") not in (None, record.namespace):
                continue
            return index
        return None

    def _apply_to_containers(self, containers: List, values: dict) -> None:
        for container in containers:
            if not isinstance(container, CommentedMap):
                continue
            name = container.get("name")
            resources = container.get("resources")
            if resources is None:
                resources = CommentedMap()
                container["resources"] = resources
            requests = resources.get("requests")
            if requests is None:
                requests = CommentedMap()
                resources["requests"] = requests

            for resource, (container_filter, value) in values.items():
                if container_filter and container_filter != name:
                    continue
                requests[resource] = value
