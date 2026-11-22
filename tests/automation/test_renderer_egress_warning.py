"""Kustomize and Helm previews carry ``renderer_egress_unenforced`` (roadmap F6).

Integration: the readiness snapshot is written and read through the real
capability repository (``automation_capabilities``) and the real probe; the
plan service adds the warning to the digest-bound file plan of renderer-backed
plans only. Raw-manifest plans never call the renderer and never carry it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from greenkube.automation.plans import AutomationPlanService
from greenkube.automation.readiness import (
    STATE_ACTIVE,
    CapabilityState,
    ReadinessSnapshot,
    renderer_egress_probe,
    renderer_egress_unenforced,
)
from greenkube.gitops.contracts import RendererKind
from greenkube.gitops.planning.digest import RENDERER_EGRESS_UNENFORCED_WARNING, plan_digest
from greenkube.storage.automation_capability_repository import (
    SCOPE_GLOBAL,
    AutomationCapabilityRepository,
    CapabilityRow,
)
from tests.automation.test_plan_service import (  # pyrefly: ignore[missing-import]
    NOW,
    FakeGitBlobResolver,
    FakeKustomizePlanner,
    MemoryPlanRepository,
    _action,
    _archive_snapshot,
    _source,
)
from tests.storage.test_automation_capability_repository import db  # noqa: F401  # pyrefly: ignore[missing-import]


async def _report(manager, reason: str | None, enforced: bool | None) -> None:
    snapshot = ReadinessSnapshot(
        refreshed_at=datetime.now(timezone.utc),
        capabilities={"renderer": CapabilityState(STATE_ACTIVE, reason, {"egress_enforced": enforced})},
    )
    await AutomationCapabilityRepository(manager).replace_snapshot(snapshot.rows())


def _service(probe) -> tuple[AutomationPlanService, FakeKustomizePlanner]:
    planner = FakeKustomizePlanner()
    service = AutomationPlanService(
        MemoryPlanRepository(),
        git_blob_resolver=FakeGitBlobResolver(),
        planners={RendererKind.KUSTOMIZE: planner},
        clock=lambda: NOW,
        renderer_egress_unenforced=probe,
    )
    return service, planner


def _kustomize_source():
    return _source(renderer=RendererKind.KUSTOMIZE, renderer_config={"overlay_path": "apps/payments"})


async def test_kustomize_preview_carries_the_warning_when_the_canary_connected(db, monkeypatch):  # noqa: F811
    await _report(db, "renderer_egress_unenforced", False)
    service, _ = _service(renderer_egress_probe(db))
    source = _kustomize_source()

    plan = await service.preview_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    file_plan = plan.file_plan
    assert file_plan is not None
    assert file_plan.warnings == [RENDERER_EGRESS_UNENFORCED_WARNING]
    assert file_plan.warnings[0].startswith("renderer_egress_unenforced:")
    # The warning is bound by the digest the approver echoes back.
    assert plan.digest == file_plan.digest != f"sha256:{'d' * 64}"
    assert file_plan.digest == plan_digest(
        plan_id=file_plan.plan_id,
        action=_action(),
        source=source,
        files=file_plan.files,
        diff=file_plan.diff,
        rendered_diff=file_plan.rendered_diff,
        base_revision=file_plan.base_revision,
        source_revision=file_plan.source_revision,
        validation=file_plan.validation,
        warnings=file_plan.warnings,
        renderer=file_plan.renderer,
        error_type=ValueError,
    )


async def test_no_warning_when_the_egress_policy_is_enforced(db, monkeypatch):  # noqa: F811
    await _report(db, None, True)
    service, _ = _service(renderer_egress_probe(db))
    source = _kustomize_source()

    plan = await service.preview_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    assert plan.file_plan is not None and plan.file_plan.warnings == []
    assert plan.digest == f"sha256:{'d' * 64}"


async def test_raw_manifest_plans_never_carry_the_renderer_warning(db, monkeypatch):  # noqa: F811
    await _report(db, "renderer_egress_unenforced", False)
    service, _ = _service(renderer_egress_probe(db))
    source = _source()

    plan = await service.preview_plan(_action(), source, _archive_snapshot(monkeypatch, source))

    assert plan.file_plan is not None
    assert RENDERER_EGRESS_UNENFORCED_WARNING not in plan.file_plan.warnings


@pytest.mark.asyncio
async def test_an_unreadable_state_adds_no_warning(monkeypatch):
    async def broken() -> bool:
        raise ConnectionError("database down")

    service, _ = _service(broken)
    source = _kustomize_source()
    plan = await service.preview_plan(_action(), source, _archive_snapshot(monkeypatch, source))
    assert plan.file_plan is not None and plan.file_plan.warnings == []


def test_capability_rows_are_read_by_reason_or_egress_flag():
    assert renderer_egress_unenforced(None) is False
    assert renderer_egress_unenforced(CapabilityRow(SCOPE_GLOBAL, "renderer", "active", {"reason": None})) is False
    assert renderer_egress_unenforced(
        CapabilityRow(SCOPE_GLOBAL, "renderer", "active", {"reason": "renderer_egress_unenforced"})
    )
    assert renderer_egress_unenforced(CapabilityRow(SCOPE_GLOBAL, "renderer", "active", {"egress_enforced": False}))
