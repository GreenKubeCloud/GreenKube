"""Pull-request body templates (roadmap J1).

A template is Markdown with Jinja2-style ``{{ name }}`` placeholders. It is
deliberately logic-free: an operator-supplied template (``automation.pr.template``)
can reorder, drop or wrap the rendered sections and add its own text, but it
cannot execute code. Unknown placeholders are rejected when the template is
loaded, so a typo fails at startup instead of leaking ``{{ ... }}`` into PRs.

The machine-readable ``greenkube:v1`` footer is never part of the template: it
is always appended after rendering so automation can find the plan again.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

#: Placeholder syntax: ``{{ name }}`` with optional inner spaces.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_UNTERMINATED = re.compile(r"\{\{(?![^{}]*\}\})")
MAX_TEMPLATE_CHARS = 32_000

#: Rendered sections, in the order of the default template (J1 sections 1-8).
SECTION_NAMES: tuple[str, ...] = (
    "summary",
    "changes",
    "why",
    "risk",
    "assumptions",
    "rollback",
    "verification",
    "provenance",
    "diffs",
)
#: Sections that truncation never removes (J1 acceptance: sections 1-7).
ESSENTIAL_SECTIONS: tuple[str, ...] = SECTION_NAMES[:7]
#: Scalar values a template may also reference.
SCALAR_NAMES: tuple[str, ...] = (
    "title",
    "plan_id",
    "digest",
    "workload",
    "namespace",
    "repository",
    "branch",
    "recommendation_ids",
    "risk_level",
    "plan_url",
)
KNOWN_NAMES = frozenset(SECTION_NAMES + SCALAR_NAMES)

DEFAULT_TEMPLATE = """{{ summary }}

{{ changes }}

{{ why }}

{{ risk }}

{{ assumptions }}

{{ rollback }}

{{ verification }}

{{ provenance }}

{{ diffs }}
"""


class PullRequestTemplateError(ValueError):
    """An operator-supplied pull-request template is invalid."""


def validate_template(template: str) -> str:
    """Return ``template`` when it only references known placeholders."""
    if not isinstance(template, str) or not template.strip():
        raise PullRequestTemplateError("The pull-request template must be non-empty text.")
    if len(template) > MAX_TEMPLATE_CHARS:
        raise PullRequestTemplateError(f"The pull-request template exceeds {MAX_TEMPLATE_CHARS} characters.")
    if "\x00" in template:
        raise PullRequestTemplateError("The pull-request template contains a NUL character.")
    unknown = sorted({match.group(1) for match in PLACEHOLDER.finditer(template)} - KNOWN_NAMES)
    if unknown:
        raise PullRequestTemplateError(
            "The pull-request template references unknown placeholder(s): "
            + ", ".join(unknown)
            + ". Known: "
            + ", ".join(sorted(KNOWN_NAMES))
            + "."
        )
    if _UNTERMINATED.search(PLACEHOLDER.sub("", template)):
        raise PullRequestTemplateError("The pull-request template contains an unterminated '{{' placeholder.")
    return template


def load_template(value: str | None) -> str:
    """The configured template, or the default one when ``value`` is blank."""
    if value is None or not value.strip():
        return DEFAULT_TEMPLATE
    return validate_template(value)


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Substitute every placeholder; values are inserted verbatim, never re-expanded.

    Blank lines left by empty sections are collapsed in the template text only,
    before the values go in, so diffs and other values keep their exact content.
    """
    skeleton = PLACEHOLDER.sub(lambda match: match.group(0) if values.get(match.group(1)) else "", template)
    skeleton = _BLANK_RUN.sub("\n\n", skeleton)
    rendered = PLACEHOLDER.sub(lambda match: values.get(match.group(1), ""), skeleton)
    return rendered.strip() + "\n"


_BLANK_RUN = re.compile(r"\n(?:[ \t]*\n){2,}")
