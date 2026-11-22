# src/greenkube/automation/git/gitlab.py
"""GitLab REST API adapter (GitLab.com and self-managed)."""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import replace
from typing import List, Literal, Optional
from urllib.parse import quote

from greenkube.automation.git.base import (
    COMMENT_PAGE_SIZE,
    MAX_CODEOWNER_REVIEWERS,
    CommitStatusState,
    GitCommitRequest,
    GitCommitResult,
    GitConflictError,
    GitFile,
    GitFileEdit,
    GitProvider,
    GitProviderCapability,
    GitProviderError,
    GitRepository,
    GitTree,
    GitTreeChange,
    GitTreeComparison,
    GitTreeEntry,
    MergeCommitSource,
    PullRequestComment,
    PullRequestInfo,
    PullRequestOptions,
    _git_response_object,
    _validate_full_commit_sha,
    is_full_commit_sha,
    normalize_open_pull_request_branch_match,
    normalize_open_pull_request_match,
    normalize_pull_request,
    normalize_terminal_pull_request_match,
    parse_comment_id,
    parse_comment_list,
    parse_pull_request_list,
    pull_request_next_page,
    validate_git_ref,
)

logger = logging.getLogger(__name__)
_GITLAB_STATUS_STATES = {"pending": "pending", "success": "success", "failure": "failed", "error": "failed"}

#: CODEOWNERS username → user ID lookups, shared by every adapter of the process.
#: Keyed by API base URL and username; unknown users are cached too.
_USER_ID_CACHE: OrderedDict[tuple[str, str], tuple[Optional[int], float]] = OrderedDict()
_USER_ID_CACHE_SIZE = 1_024
_USER_ID_CACHE_TTL_SECONDS = 3_600.0
#: Lookups one CODEOWNERS mapping may make, cached answers excluded.
_MAX_USER_LOOKUPS = 2 * MAX_CODEOWNER_REVIEWERS


class GitLabProvider(GitProvider):
    """Opens merge requests through the GitLab REST API."""

    name = "gitlab"
    capabilities = frozenset(
        {
            GitProviderCapability.MULTI_FILE_COMMIT,
            GitProviderCapability.FIND_PULL_REQUEST,
            GitProviderCapability.GET_REF_REVISION,
            GitProviderCapability.REPOSITORY_ARCHIVE,
            GitProviderCapability.COMPLETE_FILE_TREE,
            GitProviderCapability.DRAFT_PULL_REQUEST,
            GitProviderCapability.LABELS,
            GitProviderCapability.REVIEWERS,
            GitProviderCapability.FIND_OPEN_PULL_REQUEST_FOR_BRANCH,
            GitProviderCapability.DELETE_PLAN_BRANCH,
            GitProviderCapability.COMMIT_ANCESTRY,
            GitProviderCapability.COMMIT_STATUS,
            GitProviderCapability.PULL_REQUEST_COMMENTS,
            GitProviderCapability.PULL_REQUEST_METADATA,
        }
    )

    def __init__(self, token: str, *, api_base_url: Optional[str] = None, **kwargs):
        base = api_base_url or "https://gitlab.com"
        if not base.endswith("/api/v4"):
            base = f"{base.rstrip('/')}/api/v4"
        super().__init__(token, api_base_url=base, **kwargs)

    def _headers(self) -> dict:
        return {"Accept": "application/json", "PRIVATE-TOKEN": self.token}

    def _open_pull_requests_query(self, repo: GitRepository, base: str, limit: int) -> tuple[str, dict[str, str]]:
        return (
            f"/projects/{self._project(repo)}/merge_requests",
            {
                "state": "opened",
                "target_branch": base,
                "order_by": "created_at",
                "sort": "desc",
                "per_page": str(limit),
            },
        )

    def _pull_request_commits_path(self, repo: GitRepository, number: int) -> str:
        return f"/projects/{self._project(repo)}/merge_requests/{number}/commits"

    def _project(self, repo: GitRepository) -> str:
        return quote(repo.full_name, safe="")

    def _validate_provider_pull_request_options(self, options: PullRequestOptions) -> None:
        if any(re.fullmatch(r"[1-9][0-9]*", reviewer) is None for reviewer in options.reviewers):
            raise GitProviderError("GitLab reviewers must be positive numeric user IDs.")
        if any("," in label for label in options.labels):
            raise GitProviderError("GitLab labels cannot contain commas because its API uses comma-separated names.")

    async def get_default_branch(self, repo: GitRepository) -> str:
        response = await self._request("GET", f"/projects/{self._project(repo)}")
        return response.json().get("default_branch") or repo.default_branch

    async def _repository_ref_revision(
        self,
        repo: GitRepository,
        ref: str,
        ref_type: str,
    ) -> Optional[str]:
        response = await self._request(
            "GET",
            f"/projects/{self._project(repo)}/repository/{ref_type}/{quote(ref, safe='')}",
        )
        if response.status_code == 404:
            return None
        data = _git_response_object(response, provider=self.name, ref=ref)
        commit = data.get("commit")
        revision = commit.get("id") if isinstance(commit, dict) else None
        return _validate_full_commit_sha(revision, provider=self.name, ref=ref)

    async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
        ref = self._validate_ref_request(repo, ref)
        for ref_type in ("branches", "tags"):
            revision = await self._repository_ref_revision(repo, ref, ref_type)
            if revision is not None:
                return revision
        raise GitProviderError(f"GitLab ref '{ref}' not found in {repo.full_name}.")

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> Optional[GitFile]:
        encoded_path = quote(path.lstrip("/"), safe="")
        response = await self._request(
            "GET",
            f"/projects/{self._project(repo)}/repository/files/{encoded_path}",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        data = response.json()
        import base64

        try:
            content = base64.b64decode(data.get("content", "")).decode("utf-8")
        except Exception:
            content = data.get("content", "")
        return GitFile(
            path=path,
            content=content,
            sha=data.get("last_commit_id"),
            blob_sha=data.get("blob_id"),
        )

    async def list_files(self, repo: GitRepository, ref: str) -> List[str]:
        files: List[str] = []
        page = 1
        seen_pages = set()
        while True:
            if page in seen_pages or page > 10000:
                raise GitProviderError("GitLab pagination did not progress; refusing partial results.")
            seen_pages.add(page)
            response = await self._request(
                "GET",
                f"/projects/{self._project(repo)}/repository/tree",
                params={"ref": ref, "recursive": "true", "per_page": "100", "page": str(page)},
            )
            if response.status_code == 404:
                if page == 1:
                    raise GitProviderError("GitLab repository tree was not found; refusing partial results.")
                raise GitProviderError(
                    "GitLab repository tree disappeared during pagination; refusing partial results."
                )
            data = response.json()
            files.extend(item["path"] for item in data if item.get("type") == "blob")
            next_page = response.headers.get("x-next-page")
            if not data or not next_page:
                break
            try:
                page = int(next_page)
            except (TypeError, ValueError):
                raise GitProviderError("GitLab returned an invalid pagination cursor; refusing partial results.")
        return files

    async def get_tree(self, repo: GitRepository, ref: str) -> GitTree:
        """Read the recursive tree with object IDs (paginated; used only as a fallback)."""
        entries: dict[str, GitTreeEntry] = {}
        page = 1
        seen_pages: set[int] = set()
        while True:
            if page in seen_pages or page > 10000:
                raise GitProviderError("GitLab pagination did not progress; refusing partial results.")
            seen_pages.add(page)
            response = await self._request(
                "GET",
                f"/projects/{self._project(repo)}/repository/tree",
                params={"ref": ref, "recursive": "true", "per_page": "100", "page": str(page)},
            )
            if response.status_code == 404:
                raise GitProviderError(
                    "GitLab repository tree was not found; refusing partial results.",
                    status_code=404 if page == 1 else None,
                )
            data = response.json()
            if not isinstance(data, list):
                raise GitProviderError("GitLab returned an invalid repository tree; refusing partial results.")
            for item in data:
                if not isinstance(item, dict):
                    raise GitProviderError("GitLab returned an invalid tree entry; refusing partial results.")
                item_type = item.get("type")
                if item_type == "tree":
                    continue
                path, sha, mode = item.get("path"), item.get("id"), item.get("mode")
                if (
                    item_type not in ("blob", "commit")
                    or not isinstance(path, str)
                    or not isinstance(sha, str)
                    or not sha
                    or path in entries
                ):
                    raise GitProviderError("GitLab returned an invalid tree entry; refusing partial results.")
                entries[path] = GitTreeEntry(
                    path=path,
                    sha=sha,
                    mode=mode if isinstance(mode, str) else "100644",
                    type=item_type,
                )
            next_page = response.headers.get("x-next-page")
            if not data or not next_page:
                return GitTree(entries=entries)
            try:
                page = int(next_page)
            except (TypeError, ValueError):
                raise GitProviderError("GitLab returned an invalid pagination cursor; refusing partial results.")

    async def compare_trees(self, repo: GitRepository, base_revision: str, head_ref: str) -> GitTreeComparison:
        """Diff ``base_revision..head_ref`` with one direct compare call (paths only, no blob IDs)."""
        base_revision = _validate_full_commit_sha(base_revision, provider=self.name, ref=base_revision)
        response = await self._request(
            "GET",
            f"/projects/{self._project(repo)}/repository/compare",
            params={"from": base_revision, "to": head_ref, "straight": "true"},
        )
        if response.status_code == 404:
            raise GitProviderError(
                f"GitLab could not compare {base_revision} with {head_ref!r} in {repo.full_name}.", status_code=404
            )
        data = _git_response_object(response, provider=self.name, ref=head_ref)
        diffs = data.get("diffs")
        if data.get("compare_timeout") is True or not isinstance(diffs, list):
            # GitLab could not compute a complete diff: fall back to full trees.
            return await super().compare_trees(repo, base_revision, head_ref)

        statuses: dict[str, Literal["added", "modified", "deleted"]] = {}

        def record(path: object, status: Literal["added", "modified", "deleted"]) -> None:
            if not isinstance(path, str):
                raise GitProviderError("GitLab returned an invalid compare entry.")
            GitFileEdit(path=path, action="delete")
            previous = statuses.get(path)
            statuses[path] = status if previous is None or previous == status else "modified"

        for diff in diffs:
            if not isinstance(diff, dict):
                raise GitProviderError("GitLab returned an invalid compare entry.")
            old_path, new_path = diff.get("old_path"), diff.get("new_path")
            if diff.get("deleted_file") is True:
                record(old_path, "deleted")
            elif diff.get("new_file") is True:
                record(new_path, "added")
            elif diff.get("renamed_file") is True or old_path != new_path:
                record(old_path, "deleted")
                record(new_path, "added")
            else:
                record(new_path, "modified")

        commit = data.get("commit")
        head_revision = commit.get("id") if isinstance(commit, dict) else None
        if not is_full_commit_sha(head_revision):
            head_revision = head_ref if is_full_commit_sha(head_ref) else None
        changes = tuple(GitTreeChange(path=path, status=status) for path, status in sorted(statuses.items()))
        return GitTreeComparison(
            base_revision=base_revision,
            head_ref=head_ref,
            changes=changes,
            head_revision=head_revision,
        )

    async def is_ancestor(self, repo: GitRepository, ancestor_sha: str, descendant_sha: str) -> Optional[bool]:
        ancestor, descendant = self._validate_ancestry_request(repo, ancestor_sha, descendant_sha)
        if ancestor == descendant:
            return True
        response = await self._request(
            "GET",
            f"/projects/{self._project(repo)}/repository/merge_base",
            params={"refs[]": [ancestor, descendant]},
        )
        if response.status_code == 404:
            return None
        data = _git_response_object(response, provider=self.name, ref=f"{ancestor}...{descendant}")
        merge_base = data.get("id")
        if not is_full_commit_sha(merge_base):
            return None
        return merge_base.lower() == ancestor

    async def _project_merge_method(self, repo: GitRepository) -> Optional[str]:
        response = await self._request("GET", f"/projects/{self._project(repo)}")
        if response.status_code == 404:
            return None
        data = _git_response_object(response, provider=self.name, ref=repo.full_name)
        merge_method = data.get("merge_method")
        return merge_method if isinstance(merge_method, str) else None

    async def _with_landed_commit(self, repo: GitRepository, pull_request: PullRequestInfo) -> PullRequestInfo:
        """Resolve the commit that landed for merges without a merge commit (BUG-46).

        Squash merges expose ``squash_commit_sha``; fast-forward merges leave
        both SHAs empty and land the merge request head (``sha``) itself.
        """
        if not pull_request.merged or pull_request.merge_commit_sha is not None:
            return pull_request
        raw = pull_request.raw
        squash_sha = raw.get("squash_commit_sha")
        head_sha = raw.get("sha")
        if is_full_commit_sha(squash_sha):
            landed, source = squash_sha, MergeCommitSource.SQUASH_COMMIT
        elif raw.get("squash") is not True and is_full_commit_sha(head_sha):
            if await self._project_merge_method(repo) != "ff":
                return pull_request
            landed, source = head_sha, MergeCommitSource.FAST_FORWARD_HEAD
        else:
            return pull_request
        logger.info(
            "GitLab merge request !%d in %s merged without a merge commit; using its %s.",
            pull_request.number,
            repo.full_name,
            source.value,
        )
        return replace(pull_request, merge_commit_sha=landed, merge_commit_source=source.value)

    async def get_archive(self, repo: GitRepository, revision: str, *, max_bytes: int) -> bytes:
        revision = self._validate_archive_request(repo, revision, max_bytes)
        return await self._request_archive(
            f"/projects/{self._project(repo)}/repository/archive.tar.gz",
            params={"sha": revision},
            max_bytes=max_bytes,
        )

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        branch = validate_git_ref(branch, field="branch name")
        from_ref = validate_git_ref(from_ref, field="base branch")
        response = await self._request(
            "POST",
            f"/projects/{self._project(repo)}/repository/branches",
            params={"branch": branch, "ref": from_ref},
            ok_statuses=(200, 201, 400),
        )
        if response.status_code == 400 and "already exists" in response.text.lower():
            logger.info("Branch %s already exists in %s; reusing it.", branch, repo.full_name)
            return
        if response.status_code not in (200, 201):
            raise GitProviderError(f"Could not create branch '{branch}': {response.text[:200]}")

    async def create_commit(self, request: GitCommitRequest) -> GitCommitResult:
        self._validate_commit_request(request)
        repo = request.repo
        branch = validate_git_ref(request.branch, field="branch name")
        branch_path = f"/projects/{self._project(repo)}/repository/branches/{quote(branch, safe='')}"
        branch_response = await self._request("GET", branch_path, conflict_statuses=(404,))
        branch_data = branch_response.json()
        current_head = branch_data.get("commit", {}).get("id") if isinstance(branch_data, dict) else None
        if not isinstance(current_head, str) or not current_head:
            raise GitProviderError(f"GitLab returned an invalid branch revision for {repo.full_name}/{branch}.")
        if current_head != request.base_revision:
            raise GitConflictError(
                f"Branch '{branch}' moved from base revision {request.base_revision} to {current_head}."
            )

        file_states = await self._validate_commit_edits(request)
        actions = []
        for edit in request.files:
            action = {"action": edit.action, "file_path": edit.path}
            if edit.content is not None:
                action["content"] = edit.content
                action["encoding"] = "text"
            if edit.action in ("update", "delete"):
                current = file_states[edit.path]
                if current is None or not current.sha:
                    raise GitProviderError(
                        f"GitLab did not return a last-commit revision for '{edit.path}'; refusing the commit."
                    )
                action["last_commit_id"] = current.sha
            actions.append(action)

        # No ``start_sha``: GitLab uses it only to create ``branch`` and refuses it on an
        # existing branch ("A branch called ... already exists"). The plan branch exists
        # already: its head was checked above, ``last_commit_id`` guards each edited file,
        # and the parent of the new commit is checked against the base revision below.
        commit_response = await self._request(
            "POST",
            f"/projects/{self._project(repo)}/repository/commits",
            json_body={
                "branch": branch,
                "force": False,
                "commit_message": request.message,
                "author_name": request.author_name,
                "author_email": request.author_email,
                "actions": actions,
            },
            ok_statuses=(200, 201, 204, 400),
            conflict_statuses=(404, 409, 422),
            retryable=False,
        )
        if commit_response.status_code == 400:
            try:
                data = commit_response.json()
            except ValueError:
                data = None
            detail = (
                str(data.get("message") or data.get("error") or commit_response.text[:300])
                if isinstance(data, dict)
                else commit_response.text[:300]
            )
            lowered_detail = detail.lower()
            if any(
                marker in lowered_detail
                for marker in (
                    "already exists",
                    "already been taken",
                    "changed since",
                    "conflict",
                    "does not exist",
                    "doesn't exist",
                    "fast-forward",
                    "last_commit_id",
                )
            ):
                raise GitConflictError(f"GitLab rejected the commit because the base revision changed: {detail}")
            raise GitProviderError(f"GitLab could not create the atomic commit: {detail}")

        commit_data = commit_response.json()
        commit_sha = commit_data.get("id") if isinstance(commit_data, dict) else None
        if not isinstance(commit_sha, str) or not commit_sha:
            raise GitProviderError("GitLab returned an invalid commit SHA.")
        # Without ``start_sha`` this is the proof the commit sits on the base revision: a missing
        # or malformed parent list fails closed.
        parent_ids = commit_data.get("parent_ids")
        if not isinstance(parent_ids, list) or request.base_revision not in parent_ids:
            raise GitConflictError(
                f"GitLab created commit {commit_sha} with an unexpected parent; expected {request.base_revision}."
            )
        return GitCommitResult(
            commit_sha=commit_sha,
            branch=branch,
            files_changed=[edit.path for edit in request.files],
        )

    async def create_pull_request(
        self,
        repo: GitRepository,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequestInfo:
        selected_options = self.validate_pull_request_options(options)
        if repo.provider != self.name:
            raise GitProviderError(f"Pull-request creation targets provider '{repo.provider}', not '{self.name}'.")
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        if selected_options.draft and not title.casefold().startswith(("draft:", "wip:")):
            title = f"Draft: {title}"
        request_body: dict[str, object] = {
            "source_branch": head,
            "target_branch": base,
            "title": title,
            "description": body,
        }
        if selected_options.labels:
            request_body["labels"] = ",".join(selected_options.labels)
        if selected_options.reviewers:
            request_body["reviewer_ids"] = [int(reviewer) for reviewer in selected_options.reviewers]
        response = await self._request(
            "POST",
            f"/projects/{self._project(repo)}/merge_requests",
            json_body=request_body,
        )
        if response.status_code == 404:
            raise GitProviderError(f"Could not open a merge request in {repo.full_name}: project or branch not found.")
        return normalize_pull_request(response.json(), repo=repo)

    async def add_pull_request_metadata(
        self,
        repo: GitRepository,
        pull_request: PullRequestInfo,
        *,
        labels: Sequence[str] = (),
        reviewers: Sequence[str] = (),
        team_reviewers: Sequence[str] = (),
    ) -> None:
        """``add_labels`` (GitLab creates missing labels) and reviewers merged with the current ones."""
        number = self._validate_pull_request_number(repo, pull_request.number)
        if any("," in label for label in labels):
            raise GitProviderError("GitLab labels cannot contain commas because its API uses comma-separated names.")
        if any(re.fullmatch(r"[1-9][0-9]*", reviewer) is None for reviewer in reviewers):
            raise GitProviderError("GitLab reviewers must be positive numeric user IDs.")
        body: dict[str, object] = {}
        if labels:
            body["add_labels"] = ",".join(labels)
        if reviewers:
            raw = pull_request.raw if isinstance(pull_request.raw, dict) else {}
            current = raw.get("reviewers")
            if not isinstance(current, list):
                current = (await self.get_pull_request(repo, number)).raw.get("reviewers")
            current_ids = [
                item["id"]
                for item in (current if isinstance(current, list) else [])
                if isinstance(item, dict) and isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool)
            ]
            merged = list(dict.fromkeys([*current_ids, *(int(reviewer) for reviewer in reviewers)]))
            if merged != current_ids:
                body["reviewer_ids"] = merged
        if not body:
            return
        response = await self._request(
            "PUT", f"/projects/{self._project(repo)}/merge_requests/{number}", json_body=body
        )
        if response.status_code == 404:
            raise GitProviderError(f"GitLab could not update merge request {number} in {repo.full_name}.")

    async def codeowner_reviewers(
        self,
        repo: GitRepository,
        owners: Sequence[str],
    ) -> tuple[list[str], list[str]]:
        """GitLab reviewers are user IDs: ``@username`` owners are looked up, groups are skipped."""
        reviewer_ids: list[str] = []
        lookups = 0
        for owner in owners:
            if len(reviewer_ids) >= MAX_CODEOWNER_REVIEWERS:
                break
            if not owner.startswith("@") or "/" in owner or len(owner) < 2:
                continue
            key = (self.api_base_url or "", owner[1:].casefold())
            cached = _USER_ID_CACHE.get(key)
            if cached is not None and cached[1] > time.monotonic():
                _USER_ID_CACHE.move_to_end(key)
                user_id = cached[0]
            elif lookups >= _MAX_USER_LOOKUPS:
                continue
            else:
                lookups += 1
                user_id = await self._lookup_user_id(owner[1:])
                _USER_ID_CACHE[key] = (user_id, time.monotonic() + _USER_ID_CACHE_TTL_SECONDS)
                _USER_ID_CACHE.move_to_end(key)
                while len(_USER_ID_CACHE) > _USER_ID_CACHE_SIZE:
                    _USER_ID_CACHE.popitem(last=False)
            if user_id is not None and str(user_id) not in reviewer_ids:
                reviewer_ids.append(str(user_id))
        return reviewer_ids, []

    async def _lookup_user_id(self, username: str) -> Optional[int]:
        response = await self._request("GET", "/users", params={"username": username})
        if response.status_code == 404:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        user = data[0] if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None
        user_id = user.get("id") if user is not None else None
        if isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0:
            return user_id
        return None

    async def set_commit_status(
        self,
        repo: GitRepository,
        sha: str,
        *,
        state: CommitStatusState,
        context: str,
        description: str,
        target_url: Optional[str] = None,
    ) -> None:
        sha = self._validate_commit_status(repo, sha, state, context)
        payload: dict[str, str] = {
            "state": _GITLAB_STATUS_STATES[state],
            "name": context,
            "description": description[:255],
        }
        if target_url:
            payload["target_url"] = target_url
        response = await self._request(
            "POST", f"/projects/{self._project(repo)}/statuses/{quote(sha, safe='')}", json_body=payload
        )
        if response.status_code == 404:
            raise GitProviderError(f"GitLab could not set a commit status on {sha} in {repo.full_name}.")

    async def _list_pull_request_comments(
        self, repo: GitRepository, number: int, page: int
    ) -> tuple[list[PullRequestComment], bool]:
        response = await self._request(
            "GET",
            f"/projects/{self._project(repo)}/merge_requests/{number}/notes",
            params={"per_page": str(COMMENT_PAGE_SIZE), "page": str(page), "sort": "asc", "order_by": "created_at"},
        )
        if response.status_code == 404:
            raise GitProviderError(f"GitLab could not list notes of merge request {number}.")
        comments = parse_comment_list(response, provider=self.name)
        next_page = response.headers.get("x-next-page")
        has_next = bool(next_page) and next_page != "0" if next_page is not None else len(comments) >= COMMENT_PAGE_SIZE
        return comments, has_next

    async def _create_pull_request_comment(self, repo: GitRepository, number: int, body: str) -> int:
        response = await self._request(
            "POST",
            f"/projects/{self._project(repo)}/merge_requests/{number}/notes",
            json_body={"body": body},
            retryable=False,
        )
        return parse_comment_id(response, provider=self.name)

    async def _update_pull_request_comment(self, repo: GitRepository, number: int, comment_id: int, body: str) -> None:
        response = await self._request(
            "PUT",
            f"/projects/{self._project(repo)}/merge_requests/{number}/notes/{comment_id}",
            json_body={"body": body},
        )
        if response.status_code == 404:
            raise GitProviderError(f"GitLab note {comment_id} disappeared before it could be updated.")

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise GitProviderError("Pull-request number must be a positive integer.")
        response = await self._request("GET", f"/projects/{self._project(repo)}/merge_requests/{number}")
        if response.status_code == 404:
            raise GitProviderError(f"Merge request {number} not found in {repo.full_name}.")
        return await self._with_landed_commit(repo, normalize_pull_request(response.json(), repo=repo))

    async def find_terminal_pull_request(self, repo: GitRepository, head: str, base: str) -> Optional[PullRequestInfo]:
        """The most recently updated merged or closed merge request from ``head`` into ``base`` (D10)."""
        if repo.provider != self.name:
            raise GitProviderError(f"Pull-request lookup targets provider '{repo.provider}', not '{self.name}'.")
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        for state in ("merged", "closed"):
            response = await self._request(
                "GET",
                f"/projects/{self._project(repo)}/merge_requests",
                params={
                    "state": state,
                    "source_branch": head,
                    "target_branch": base,
                    "order_by": "updated_at",
                    "sort": "desc",
                    "per_page": "100",
                },
            )
            if response.status_code == 404:
                raise GitProviderError(f"Could not list merge requests in {repo.full_name}: project not found.")
            for item in parse_pull_request_list(response, repo=repo):
                match = normalize_terminal_pull_request_match(
                    item,
                    repo=repo,
                    head=head,
                    base=base,
                    provider_head=item.get("source_branch"),
                    provider_base=item.get("target_branch"),
                    open_states=("opened",),
                )
                if match is not None:
                    return match
        return None

    async def find_pull_request(self, repo: GitRepository, head: str, base: str) -> Optional[PullRequestInfo]:
        if repo.provider != self.name:
            raise GitProviderError(f"Pull-request lookup targets provider '{repo.provider}', not '{self.name}'.")
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        page_size = 100
        page = 1
        while page <= 10000:
            response = await self._request(
                "GET",
                f"/projects/{self._project(repo)}/merge_requests",
                params={
                    "state": "opened",
                    "source_branch": head,
                    "target_branch": base,
                    "per_page": str(page_size),
                    "page": str(page),
                },
            )
            if response.status_code == 404:
                raise GitProviderError(f"Could not list merge requests in {repo.full_name}: project not found.")
            data = parse_pull_request_list(response, repo=repo)
            for item in data:
                match = normalize_open_pull_request_match(
                    item,
                    repo=repo,
                    head=head,
                    base=base,
                    provider_head=item.get("source_branch"),
                    provider_base=item.get("target_branch"),
                    open_states=("opened",),
                )
                if match is not None:
                    return match

            next_page = response.headers.get("x-next-page")
            if next_page == "0" or next_page == "":
                return None
            if next_page is None:
                linked_page = pull_request_next_page(response, repo=repo, current_page=page)
                if linked_page is not None:
                    page = linked_page
                    continue
                if len(data) < page_size:
                    return None
                page += 1
                continue
            try:
                next_page_number = int(next_page)
            except ValueError as exc:
                raise GitProviderError("GitLab returned an invalid pull-request pagination cursor.") from exc
            if next_page_number <= page:
                raise GitProviderError("GitLab pull-request pagination did not progress.")
            page = next_page_number
        raise GitProviderError("GitLab pull-request pagination exceeded the safety limit.")

    async def find_open_pull_request_for_branch(
        self,
        repo: GitRepository,
        branch: str,
    ) -> Optional[PullRequestInfo]:
        branch = self._validate_plan_branch_request(repo, branch)
        page_size = 100
        for branch_filter in ("source_branch", "target_branch"):
            page = 1
            while page <= 10000:
                response = await self._request(
                    "GET",
                    f"/projects/{self._project(repo)}/merge_requests",
                    params={
                        "state": "opened",
                        branch_filter: branch,
                        "per_page": str(page_size),
                        "page": str(page),
                    },
                )
                if response.status_code == 404:
                    raise GitProviderError(f"Could not list merge requests in {repo.full_name}: project not found.")
                data = parse_pull_request_list(response, repo=repo)
                for item in data:
                    match = normalize_open_pull_request_branch_match(
                        item,
                        repo=repo,
                        branch=branch,
                        provider_head=item.get("source_branch"),
                        provider_base=item.get("target_branch"),
                        open_states=("opened",),
                    )
                    if match is not None:
                        return match

                next_page = response.headers.get("x-next-page")
                if next_page == "0" or next_page == "":
                    break
                if next_page is None:
                    linked_page = pull_request_next_page(response, repo=repo, current_page=page)
                    if linked_page is not None:
                        page = linked_page
                        continue
                    if len(data) < page_size:
                        break
                    page += 1
                    continue
                try:
                    next_page_number = int(next_page)
                except ValueError as exc:
                    raise GitProviderError("GitLab returned an invalid pull-request pagination cursor.") from exc
                if next_page_number <= page:
                    raise GitProviderError("GitLab pull-request pagination did not progress.")
                page = next_page_number
            else:
                raise GitProviderError("GitLab pull-request pagination exceeded the safety limit.")
        return None

    async def delete_plan_branch(self, repo: GitRepository, branch: str) -> None:
        branch = self._validate_plan_branch_request(repo, branch)
        if branch == repo.default_branch:
            raise GitProviderError("Refusing to delete the plan base branch.")
        default_branch = validate_git_ref(await self.get_default_branch(repo), field="provider default branch")
        if branch == default_branch:
            raise GitProviderError("Refusing to delete the repository default branch.")
        if await self.find_open_pull_request_for_branch(repo, branch) is not None:
            raise GitProviderError("Refusing to delete a branch with an open pull request.")
        response = await self._request(
            "DELETE",
            f"/projects/{self._project(repo)}/repository/branches/{quote(branch, safe='')}",
        )
        if response.status_code == 404:
            return
