"""The one plan digest shared by every renderer's planner (roadmap M6)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from greenkube.gitops.contracts import (
    ActionPlanV3,
    FileEdit,
    FilePlan,
    RendererKind,
    ValidationResult,
    WorkloadSource,
)

#: Warning of a Kustomize or Helm plan rendered while the renderer's egress-deny
#: NetworkPolicy is not enforced (F6): the preview still works, the approver is told.
RENDERER_EGRESS_UNENFORCED_WARNING = (
    "renderer_egress_unenforced: the cluster network plugin does not enforce the renderer's egress-deny "
    "NetworkPolicy, so this preview was rendered without network isolation (the renderer holds no Secrets "
    "and no token, Helm runs without lookup and remote Kustomize bases are refused). Use a CNI that enforces "
    "NetworkPolicy or enable renderer.egressFirewall."
)


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


def with_plan_warnings(
    file_plan: FilePlan,
    warnings: Sequence[str],
    *,
    action: ActionPlanV3,
    source: WorkloadSource,
    error_type: type[Exception],
) -> FilePlan:
    """``file_plan`` with ``warnings`` added and its digest computed again.

    Warnings are part of what an approver sees, so they are bound by the
    digest like the files: a warning added after planning (for example
    ``renderer_egress_unenforced``, F6) re-issues the plan digest. Planners
    keep their warnings sorted and unique; so does this.
    """
    merged = sorted(set(file_plan.warnings) | set(warnings))
    if merged == list(file_plan.warnings):
        return file_plan
    digest = plan_digest(
        plan_id=file_plan.plan_id,
        action=action,
        source=source,
        files=file_plan.files,
        diff=file_plan.diff,
        rendered_diff=file_plan.rendered_diff,
        base_revision=file_plan.base_revision,
        source_revision=file_plan.source_revision,
        validation=file_plan.validation,
        warnings=merged,
        renderer=file_plan.renderer,
        error_type=error_type,
    )
    return file_plan.model_copy(update={"warnings": merged, "digest": digest})
