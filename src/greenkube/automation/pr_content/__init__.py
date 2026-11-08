"""Pull-request content: titles, bodies, labels, CODEOWNERS and lifecycle comments (roadmap J1-J5)."""

from greenkube.automation.pr_content.codeowners import CODEOWNERS_PATHS, CodeOwners, parse_codeowners
from greenkube.automation.pr_content.lifecycle import (
    LIFECYCLE_MARKER,
    LifecycleState,
    MeasuredQuantity,
    render_lifecycle_comment,
)
from greenkube.automation.pr_content.metadata import PullRequestMetadata, derive_metadata
from greenkube.automation.pr_content.model import (
    PullRequestContext,
    PullRequestRenderSettings,
    external_url,
)
from greenkube.automation.pr_content.render import (
    PullRequestContent,
    parse_footer,
    render_body,
    render_commit_message,
    render_footer,
    render_pull_request,
    render_title,
)
from greenkube.automation.pr_content.template import (
    DEFAULT_TEMPLATE,
    PullRequestTemplateError,
    load_template,
    validate_template,
)

__all__ = [
    "CODEOWNERS_PATHS",
    "DEFAULT_TEMPLATE",
    "LIFECYCLE_MARKER",
    "CodeOwners",
    "LifecycleState",
    "MeasuredQuantity",
    "PullRequestContent",
    "PullRequestContext",
    "PullRequestMetadata",
    "PullRequestRenderSettings",
    "PullRequestTemplateError",
    "derive_metadata",
    "external_url",
    "load_template",
    "parse_codeowners",
    "parse_footer",
    "render_body",
    "render_commit_message",
    "render_footer",
    "render_lifecycle_comment",
    "render_pull_request",
    "render_title",
    "validate_template",
]
