"""PR content goes through the action-kind registry (roadmap H0 acceptance).

* No module outside the ``container_resources`` spec branches on
  ``isinstance(op, SetContainerResources)``.
* A kind registered outside ``src/`` labels its pull request and lists its
  changes through ``ActionKindSpec.pr_label`` / ``pr_section()`` alone.
* Container-resources pull requests stay byte-identical (golden files in
  ``snapshots/pr_bodies``, checked by ``test_pr_content.py``).
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from greenkube.automation.actions import CONTAINER_RESOURCES, PrFieldChange, PrSection
from greenkube.automation.pr_content import PullRequestContext, derive_metadata, render_body
from greenkube.automation.pr_content.model import resource_changes
from greenkube.gitops.contracts import ContainerResources, SetContainerResources, TypedOperation
from tests.automation import pr_fixtures as fx  # pyrefly: ignore[missing-import]
from tests.automation.actions.noop_kind import (  # pyrefly: ignore[missing-import]
    MinReadySecondsSpec,
    SetMinReadySeconds,
    registered,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "greenkube"
SPEC_MODULE = SRC / "automation" / "actions" / "container_resources.py"


def _isinstance_checks(path: Path) -> list[int]:
    """Lines of ``isinstance(..., SetContainerResources)`` calls (also inside tuples)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "isinstance" and len(node.args) == 2):
            continue
        names = {getattr(item, "id", getattr(item, "attr", None)) for item in ast.walk(node.args[1])}
        if "SetContainerResources" in names:
            lines.append(node.lineno)
    return lines


def test_no_container_resources_isinstance_outside_its_spec():
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if path != SPEC_MODULE and (lines := _isinstance_checks(path))
    }
    assert offenders == {}


def test_container_resources_spec_lists_requests_then_limits_sorted():
    operation = SetContainerResources(
        container="api",
        resources=ContainerResources(requests={"memory": "512Mi", "cpu": "200m"}, limits={"memory": "1Gi", "cpu": "1"}),
    )
    section = CONTAINER_RESOURCES.pr_section([operation])
    assert section.label == "rightsizing"
    assert [(change.subject, change.field, change.proposed) for change in section.changes] == [
        ("api", "requests.cpu", "200m"),
        ("api", "requests.memory", "512Mi"),
        ("api", "limits.cpu", "1"),
        ("api", "limits.memory", "1Gi"),
    ]
    assert CONTAINER_RESOURCES.pr_subject == "container"


class _LabelledMinReadySeconds(MinReadySecondsSpec):
    """The test kind with its own PR label and changes (still defined outside ``src/``)."""

    pr_label: ClassVar[str] = "rollout-tuning"
    pr_subject: ClassVar[str] = "workload"

    def subjects(self, operations: Sequence[TypedOperation]) -> set[str]:
        return {"api"} if operations else set()

    def pr_section(self, operations: Sequence[TypedOperation], recommendation: Any = None) -> PrSection:
        changes = tuple(
            PrFieldChange(subject="api", field="spec.minReadySeconds", proposed=str(operation.seconds))
            for operation in operations
            if isinstance(operation, SetMinReadySeconds)
        )
        return PrSection(label=self.pr_label, title="minReadySeconds", changes=changes)


def _context(operations) -> PullRequestContext:
    return PullRequestContext(
        plan=fx.plan(operations, baseline=False),
        repository="acme/gitops",
        branch="greenkube/plan/0123456789abcdef0123456789abcdef",
    )


def test_a_kind_registered_outside_src_labels_and_describes_its_pull_request():
    spec = _LabelledMinReadySeconds()
    with registered() as default_spec:
        from greenkube.automation.actions import ACTION_KINDS

        ACTION_KINDS.unregister(default_spec.kind)
        ACTION_KINDS.register(spec)
        try:
            context = _context([SetMinReadySeconds(seconds=30)])
            metadata = derive_metadata(context)
            changes = resource_changes(context)
            body = render_body(context)
        finally:
            ACTION_KINDS.unregister(spec.kind)
            ACTION_KINDS.register(default_spec)
    assert "optimization/rollout-tuning" in metadata.labels
    assert [(change.container, change.field, change.proposed_text) for change in changes] == [
        ("api", "spec.minReadySeconds", "30")
    ]
    assert "spec.minReadySeconds" in body
    # The kind does not support rollback: the PR says so instead of promising one.
    assert "cannot build an automated rollback" in body
