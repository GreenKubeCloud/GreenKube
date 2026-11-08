"""The one plan digest shared by every renderer's planner (roadmap M6)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from greenkube.gitops.contracts import ActionPlanV3, FileEdit, RendererKind, ValidationResult, WorkloadSource


def plan_digest(
    *,
    plan_id: str,
    action: ActionPlanV3,
    source: WorkloadSource,
    files: Sequence[FileEdit],
    diff: str,
    rendered_diff: str,
    base_revision: str,
    source_revision: str | None,
    validation: ValidationResult,
    warnings: list[str],
    renderer: RendererKind,
    error_type: type[Exception],
) -> str:
    """Return the ``sha256:`` digest that approval must echo back.

    ``files`` are hashed in the order given, so a planner that sorts its edits
    keeps the digests it produced before. Raises ``error_type`` when an input is
    not deterministically JSON-serializable.
    """
    payload = {
        "plan_id": plan_id,
        "action": action.model_dump(mode="json"),
        "binding": {
            "id": source.id,
            "provider_id": source.provider_id,
            "repo_url": source.repo_url,
            "ref": source.ref,
            "path": source.path,
            "namespace": source.namespace,
            "workload_kind": source.workload_kind,
            "workload_name": source.workload_name,
            "renderer": source.renderer.value,
            "status": source.status.value,
            "revision": source.revision,
            "source_revision": source.source_revision,
        },
        "files": [edit.model_dump(mode="json") for edit in files],
        "diff": diff,
        "rendered_diff": rendered_diff,
        "base_revision": base_revision,
        "source_revision": source_revision,
        "renderer": renderer.value,
        "validation": validation.model_dump(mode="json"),
        "warnings": warnings,
    }
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise error_type("plan inputs must be deterministically JSON-serializable") from exc
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
