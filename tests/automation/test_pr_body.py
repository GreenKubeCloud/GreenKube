from datetime import datetime, timezone

from greenkube.automation.pr_body import render_pr_body
from greenkube.models.metrics import RecommendationRecord, RecommendationType


def test_render_pr_body_escapes_adversarial_user_text():
    record = RecommendationRecord(
        id=42,
        type=RecommendationType.RIGHTSIZING_CPU,
        description="# injected heading\n[evil](https://example.invalid)\n<script>alert(1)</script>",
        reason="`close fence` | pipe *emphasis*",
        source_ref="refs/<script>\nnext",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    body = render_pr_body(record, diff="resources:\n  value: ```\n# not a heading")

    assert "\n# injected heading" not in body
    assert "[evil]" not in body
    assert "<script>" not in body
    assert body.count("```") == 2
    assert "description" not in body
    assert "\\# injected heading" in body
    assert "\\[evil\\]" in body
    assert "&lt;script&gt;" in body


def test_render_pr_body_keeps_required_sections():
    record = RecommendationRecord(
        id=7,
        type=RecommendationType.RIGHTSIZING_MEMORY,
        description="Reduce memory request",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    body = render_pr_body(record)

    for section in (
        "## Summary",
        "## Impact",
        "## Risk & reliability",
        "## Verification plan",
        "## Provenance",
        "## Reviewer checklist",
    ):
        assert section in body
