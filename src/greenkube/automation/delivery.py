# src/greenkube/automation/delivery.py
"""Delivery modes of approved plans (roadmap F4).

``automation.delivery.mode`` decides who opens the pull request of an approved
plan:

=========  ===================================================================
Mode       What happens
=========  ===================================================================
direct     GreenKube commits on the deterministic plan branch and opens the PR
           (needs write credentials for the repository).
patch      Nobody in the cluster writes to Git. The approved plan downloads as a
           ``git format-patch`` (``GET /automation/plans/{id}/patch``, or
           ``greenkube plan apply-patch <id>``); an operator pushes the branch
           and opens the PR. GreenKube links it by the deterministic branch name
           or by the ``GreenKube-Plan`` trailer of the PR body, or through the
           "I opened PR <url>" link-back.
ci         A CI job lists ``GET /automation/plans?status=approved&delivery=ci``,
           applies each patch with its own token, opens the PR and reports it
           back (``POST /automation/plans/{id}/delivery`` with the PR URL and
           head SHA). No write token lives in the cluster.
=========  ===================================================================

The delivery mode is recorded on the plan when it is approved, so a later
change of the setting never re-routes an approval. Externally delivered pull
requests are verified before they are tracked: the request must target the
plan's repository and base branch, come from the plan branch (or carry the
plan trailer in ``patch`` mode), and its head commit must hold exactly the
planned file changes. From there on, the same lifecycle tracks every mode:
``PR_OPEN`` -> ``MERGED`` -> ``SYNCED``.
"""

from __future__ import annotations

import email.utils
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import Header
from typing import Any, Literal, cast

from greenkube.automation.git.base import git_blob_id
from greenkube.gitops.contracts import Plan

logger = logging.getLogger(__name__)

DeliveryMode = Literal["direct", "patch", "ci"]

DELIVERY_DIRECT: DeliveryMode = "direct"
DELIVERY_PATCH: DeliveryMode = "patch"
DELIVERY_CI: DeliveryMode = "ci"
DELIVERY_MODES: tuple[DeliveryMode, ...] = (DELIVERY_DIRECT, DELIVERY_PATCH, DELIVERY_CI)
#: Modes in which a person or a CI job, not GreenKube, writes to Git.
EXTERNAL_DELIVERY_MODES: frozenset[str] = frozenset({DELIVERY_PATCH, DELIVERY_CI})

#: How long an approved ``patch`` or ``ci`` plan waits for its pull request after the plan TTL:
#: a person or a scheduled CI job delivers it, not GreenKube within seconds. The delivery
#: report is verified against the plan's files, so a moved base cannot slip through.
EXTERNAL_DELIVERY_GRACE = timedelta(days=7)

#: How long a patch/ci plan may sit ``APPLYING`` (an interrupted delivery recording) before
#: the automation controller resumes the recording itself.
EXTERNAL_DELIVERY_RESUME_AFTER = timedelta(minutes=5)

#: The trailer that links a commit or a pull request body to its plan.
PLAN_TRAILER = "GreenKube-Plan"
_PLAN_TRAILER_LINE = re.compile(rf"^{PLAN_TRAILER}:\s*(\S+)\s*$", re.MULTILINE)
#: ``/pull/12`` (GitHub, Gitea web), ``/pulls/12`` (Gitea), ``/-/merge_requests/12`` (GitLab).
_PULL_REQUEST_URL = re.compile(r"/(?:pull|pulls|-/merge_requests|merge_requests)/(\d{1,12})/?$")
_ZERO_BLOB = "0" * 40
_BOT_IDENTITY = "GreenKube Bot <bot@greenkube.cloud>"


class PatchUnavailableError(RuntimeError):
    """The plan's stored diff cannot be turned into a patch that applies exactly."""


def configured_delivery_mode(config: object | None = None) -> DeliveryMode:
    """The delivery mode new approvals get (``AUTOMATION_DELIVERY_MODE``); ``direct`` by default."""
    if config is None:
        from greenkube.core.config import get_config

        config = get_config()
    value = str(getattr(config, "AUTOMATION_DELIVERY_MODE", DELIVERY_DIRECT) or DELIVERY_DIRECT).strip().lower()
    for mode in DELIVERY_MODES:
        if value == mode:
            return mode
    return DELIVERY_DIRECT


def is_external(mode: str | None) -> bool:
    return mode in EXTERNAL_DELIVERY_MODES


def pull_request_number_from_url(url: str) -> int | None:
    """The request number of a GitHub, GitLab or Gitea pull-request web URL, else None."""
    if not isinstance(url, str) or len(url) > 2048 or any(char.isspace() for char in url):
        return None
    if not url.startswith(("https://", "http://")):
        return None
    match = _PULL_REQUEST_URL.search(url.split("#", 1)[0].split("?", 1)[0])
    if match is None:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def normalized_url(url: str | None) -> str:
    """Comparable form of a web URL: scheme and host lowercased, no trailing slash, query or fragment."""
    text = (url or "").strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    scheme, separator, rest = text.partition("://")
    if not separator:
        return text
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


def plan_trailer_of(text: str | None) -> str | None:
    """The plan id named by a ``GreenKube-Plan: <id>`` trailer line, else None."""
    matches = _PLAN_TRAILER_LINE.findall(text or "")
    return matches[-1] if matches else None


@dataclass(frozen=True)
class PlanPatch:
    """A plan as a ``git format-patch`` mailbox, with what is needed to apply it."""

    text: str
    branch: str
    base_ref: str
    base_revision: str
    files: tuple[str, ...]

    @property
    def filename(self) -> str:
        return "0001-greenkube-plan.patch"


def build_plan_patch(
    plan: Plan,
    *,
    branch: str,
    commit_message: str,
    date: datetime,
    author: str = _BOT_IDENTITY,
) -> PlanPatch:
    """Render an approved plan as one ``git format-patch`` commit.

    ``git am --3way`` applies it on the plan's base revision. The ``index``
    lines carry the original and planned blob ids, so a three-way merge also
    works on a later commit that left the planned files unchanged. The commit
    message keeps the ``GreenKube-Plan`` trailer, which links the pull request
    back to the plan.
    """
    file_plan = plan.file_plan
    if file_plan is None or plan.digest is None:
        raise PatchUnavailableError(
            "the plan's file contents are not available (pruned by retention or never rendered)"
        )
    sections = _diff_sections(file_plan.diff)
    edits = {edit.path: edit for edit in file_plan.files}
    if set(sections) != set(edits):
        raise PatchUnavailableError("the stored diff does not cover exactly the planned files")

    subject, _, body = commit_message.strip("\n").partition("\n")
    body = body.strip("\n")
    stat_lines: list[str] = []
    diff_parts: list[str] = []
    insertions = deletions = 0
    for path in sorted(edits):
        edit = edits[path]
        hunks = sections[path]
        _verify_section(edit.operation, edit.original_sha, edit.content, hunks)
        added = sum(1 for line in hunks if line.startswith("+"))
        removed = sum(1 for line in hunks if line.startswith("-"))
        insertions += added
        deletions += removed
        stat_lines.append(f" {path} | {added + removed} {'+' * min(added, 40)}{'-' * min(removed, 40)}")
        old_blob = (edit.original_sha or _ZERO_BLOB).lower()
        new_blob = git_blob_id(edit.content) if edit.content is not None else _ZERO_BLOB
        header = [f"diff --git a/{path} b/{path}"]
        if edit.operation == "create":
            header += ["new file mode 100644", f"index {_ZERO_BLOB}..{new_blob}", "--- /dev/null", f"+++ b/{path}"]
        elif edit.operation == "delete":
            header += ["deleted file mode 100644", f"index {old_blob}..{_ZERO_BLOB}", f"--- a/{path}", "+++ /dev/null"]
        else:
            header += [f"index {old_blob}..{new_blob}", f"--- a/{path}", f"+++ b/{path}"]
        diff_parts.append("\n".join(header) + "\n" + _terminated(hunks))

    files = len(edits)
    summary = f" {files} file{'s' if files != 1 else ''} changed"
    if insertions:
        summary += f", {insertions} insertion{'s' if insertions != 1 else ''}(+)"
    if deletions:
        summary += f", {deletions} deletion{'s' if deletions != 1 else ''}(-)"
    revision = plan.digest.split(":", 1)[-1][:40]
    lines = [
        f"From {revision} Mon Sep 17 00:00:00 2001",
        f"From: {author}",
        f"Date: {email.utils.format_datetime(date)}",
        f"Subject: {_encoded_subject(subject)}",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=UTF-8",
        "Content-Transfer-Encoding: 8bit",
        "",
    ]
    message = "\n".join(lines) + ("\n" + body + "\n" if body else "") + "---\n"
    message += "\n".join(stat_lines) + "\n" + summary + "\n\n"
    message += "".join(diff_parts)
    message += "-- \ngreenkube\n\n"
    return PlanPatch(
        text=message,
        branch=branch,
        base_ref=plan.source.ref,
        base_revision=file_plan.base_revision,
        files=tuple(sorted(edits)),
    )


def _encoded_subject(subject: str) -> str:
    text = f"[PATCH] {subject.strip() or 'GreenKube plan'}"
    if text.isascii():
        return text
    return Header(text, "utf-8").encode()


def _diff_sections(diff: str) -> dict[str, list[str]]:
    """Split a multi-file unified diff into its hunk lines per path (headers removed)."""
    sections: dict[str, list[str]] = {}
    lines = diff.splitlines(keepends=True)
    current: list[str] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if line.startswith("--- ") and following.startswith("+++ "):
            old_path = _header_path(line[4:], "a/")
            new_path = _header_path(following[4:], "b/")
            path = new_path or old_path
            if path is None or path in sections:
                raise PatchUnavailableError("the stored diff has an unexpected file header")
            current = sections.setdefault(path, [])
            index += 2
            continue
        if current is None:
            if line.strip():
                raise PatchUnavailableError("the stored diff starts outside a file section")
        elif line.startswith(("@@", "+", "-", " ", "\\")) or line in ("\n",):
            current.append(line)
        else:
            raise PatchUnavailableError("the stored diff has an unexpected line")
        index += 1
    return sections


def _header_path(value: str, prefix: str) -> str | None:
    path = value.rstrip("\n").split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    return path[len(prefix) :] if path.startswith(prefix) else path


def _terminated(lines: list[str]) -> str:
    text = "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n\\ No newline at end of file\n"
    return text


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _verify_section(operation: str, original_sha: str | None, content: str | None, hunks: Sequence[str]) -> None:
    """Fail closed unless the hunks turn the original blob into the planned content.

    The original file is rebuilt by applying the hunks in reverse to the planned
    content; its Git blob id must equal the plan's ``original_sha`` (an empty
    file for a creation), so a patch that would not apply exactly is never served.
    """
    new_lines = (content or "").splitlines(keepends=True)
    old_lines: list[str] = []
    position = 0  # next unread index in new_lines
    index = 0
    while index < len(hunks):
        header = _HUNK_HEADER.match(hunks[index])
        if header is None:
            raise PatchUnavailableError("the stored diff has a line outside a hunk")
        new_start = int(header.group(3))
        new_count = int(header.group(4)) if header.group(4) is not None else 1
        # An empty new side names the line *after which* the hunk applies.
        new_start_index = new_start if new_count == 0 else max(new_start - 1, 0)
        if new_start_index < position or new_start_index > len(new_lines):
            raise PatchUnavailableError("the stored diff hunks are out of order")
        old_lines.extend(new_lines[position:new_start_index])
        position = new_start_index
        index += 1
        while index < len(hunks) and not hunks[index].startswith("@@"):
            line = hunks[index]
            marker, text = line[:1], line[1:]
            if marker == " " or line == "\n":
                text = text if marker == " " else "\n"
                if position >= len(new_lines) or new_lines[position] != text:
                    raise PatchUnavailableError("the stored diff context does not match the planned content")
                old_lines.append(text)
                position += 1
            elif marker == "+":
                if position >= len(new_lines) or new_lines[position] != text:
                    raise PatchUnavailableError("the stored diff additions do not match the planned content")
                position += 1
            elif marker == "-":
                old_lines.append(text)
            elif marker == "\\":
                if old_lines and old_lines[-1].endswith("\n") and hunks[index - 1].startswith(("-", " ")):
                    old_lines[-1] = old_lines[-1][:-1]
            else:
                raise PatchUnavailableError("the stored diff has an unexpected hunk line")
            index += 1
    old_lines.extend(new_lines[position:])
    original = "".join(old_lines)
    if operation == "create":
        if original:
            raise PatchUnavailableError("the stored diff of a new file does not start from an empty file")
        return
    if not original_sha:
        raise PatchUnavailableError("the plan does not record the original blob of an edited file")
    object_format = "sha256" if len(original_sha) == 64 else "sha1"
    if git_blob_id(original, object_format=object_format) != original_sha.lower():
        raise PatchUnavailableError(f"the stored diff does not reproduce the original blob of {original_sha[:12]}")


def effective_expiry(plan: Plan) -> datetime | None:
    """When ``plan`` expires: its TTL, plus :data:`EXTERNAL_DELIVERY_GRACE` once approved for patch or ci."""
    from greenkube.gitops.contracts import PlanStatus

    if plan.expires_at is None:
        return None
    if plan.status is PlanStatus.APPROVED and is_external(plan.delivery_mode):
        return plan.expires_at + EXTERNAL_DELIVERY_GRACE
    return plan.expires_at


# --------------------------------------------------------------------------- trailer linkage (F4)

#: Pull requests whose commits are read per linkage pass (each costs one provider call).
MAX_COMMIT_SCANS_PER_PASS = 10
#: Pull requests already tried and refused are skipped until their head changes (bounded memory).
_REFUSED_CACHE_SIZE = 512


def plan_ids_in_pull_request_body(body: str | None) -> set[str]:
    """Plan ids named by a pull request description: the trailer or the GreenKube footer."""
    ids: set[str] = set()
    if not body:
        return ids
    trailer = plan_trailer_of(body)
    if trailer:
        ids.add(trailer)
    try:
        from greenkube.automation.pr_content.render import parse_footer

        footer = parse_footer(body)
    except Exception:
        footer = None
    plan_id = footer.get("plan_id") if isinstance(footer, dict) else None
    if isinstance(plan_id, str) and plan_id:
        ids.add(plan_id)
    return ids


def plan_ids_in_commit_messages(messages: Sequence[str]) -> set[str]:
    """Plan ids named by the ``GreenKube-Plan`` trailer of any commit message."""
    return {plan_id for plan_id in (plan_trailer_of(message) for message in messages) if plan_id}


class PlanTrailerLinker:
    """Links ``patch``-mode pull requests opened from any branch by the ``GreenKube-Plan`` trailer.

    The automation controller already links a pull request opened from the
    deterministic plan branch. An operator who applies the patch with
    ``git am`` on a branch of their own keeps the trailer in the commit
    message, and often in the description. Each pass lists the open pull
    requests targeting the base branch of the approved ``patch`` plans (one
    page per repository and base branch), reads the description, and the
    commit messages of at most :data:`MAX_COMMIT_SCANS_PER_PASS` requests,
    then records a match through the worker's verified delivery report (same
    checks as ``POST /automation/plans/{id}/delivery``). Read access only.
    """

    def __init__(self, worker: Any, *, max_commit_scans: int = MAX_COMMIT_SCANS_PER_PASS) -> None:
        self.worker = worker
        self.max_commit_scans = max_commit_scans
        #: (plan id, PR number, head sha) already refused, or PRs whose commits were read.
        self._seen: dict[tuple[str, int, str | None], None] = {}

    def _remember(self, key: tuple[str, int, str | None]) -> None:
        self._seen[key] = None
        while len(self._seen) > _REFUSED_CACHE_SIZE:
            self._seen.pop(next(iter(self._seen)))

    async def link_once(self) -> int:
        from greenkube.gitops.contracts import PlanStatus

        lister = getattr(getattr(self.worker, "plans", None), "list_plans", None)
        resolver = getattr(self.worker, "provider_resolver", None)
        if not callable(lister) or resolver is None:
            return 0
        plans = [
            plan
            for plan in await cast(Any, lister)(status=PlanStatus.APPROVED)
            if plan.delivery_mode == DELIVERY_PATCH and plan.digest
        ]
        groups: dict[tuple[str, str, str], list[Plan]] = {}
        for plan in plans:
            groups.setdefault((plan.source.provider_id, plan.source.repo_url, plan.source.ref), []).append(plan)
        linked = 0
        scans = 0
        for group in groups.values():
            by_id = {plan.plan_id: plan for plan in group}
            try:
                target = await resolver.resolve(group[0].source)
            except Exception as error:
                logger.info("Trailer linkage: provider unavailable (%s).", type(error).__name__)
                continue
            try:
                pull_requests = await target.provider.list_open_pull_requests(target.repository, group[0].source.ref)
                for pull_request in pull_requests:
                    raw = pull_request.raw if isinstance(pull_request.raw, dict) else {}
                    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
                    head_sha = head.get("sha") if isinstance(head, dict) else raw.get("sha")
                    head_sha = head_sha if isinstance(head_sha, str) else None
                    body = raw.get("body") if isinstance(raw.get("body"), str) else raw.get("description")
                    candidates = plan_ids_in_pull_request_body(body if isinstance(body, str) else None) & set(by_id)
                    if not candidates and scans < self.max_commit_scans:
                        scan_key = ("", pull_request.number, head_sha)
                        if scan_key not in self._seen:
                            scans += 1
                            messages = await target.provider.list_pull_request_commit_messages(
                                target.repository, pull_request.number
                            )
                            candidates = plan_ids_in_commit_messages(messages) & set(by_id)
                            # Read once per head: a refused match is remembered per plan below.
                            self._remember(scan_key)
                    for plan_id in sorted(candidates):
                        key = (plan_id, pull_request.number, head_sha)
                        if key in self._seen:
                            continue
                        if await self._record(by_id[plan_id], pull_request.url, head_sha):
                            linked += 1
                            by_id.pop(plan_id, None)
                        else:
                            self._remember(key)
            except Exception as error:
                logger.info("Trailer linkage: pull-request scan failed (%s).", type(error).__name__)
            finally:
                try:
                    await target.provider.close()
                except Exception:
                    pass
        return linked

    async def _record(self, plan: Plan, pr_url: str, head_sha: str | None) -> bool:
        try:
            await self.worker.record_external_delivery(
                plan.plan_id, pr_url=pr_url, head_sha=head_sha, actor="automation-controller"
            )
        except Exception as error:
            code = getattr(error, "code", type(error).__name__)
            logger.info("Plan %s: pull request %s not linked by trailer (%s).", plan.plan_id, pr_url, code)
            return False
        logger.info("Plan %s linked to pull request %s by its GreenKube-Plan trailer.", plan.plan_id, pr_url)
        return True
