# src/greenkube/automation/git/gitea.py
"""Gitea/Forgejo REST API adapter (self-hosted, GitHub-inspired)."""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from typing import List, Literal, Optional
from urllib.parse import quote
from uuid import uuid4

from greenkube.automation.git.base import (
    COMMENT_PAGE_SIZE,
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
    PullRequestComment,
    PullRequestInfo,
    PullRequestOptions,
    _git_response_data,
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
    quote_git_path,
    validate_git_ref,
)

logger = logging.getLogger(__name__)
_LABEL_COLOR = "#2e7d32"

MAX_REPOSITORY_FILES = 20_000
MAX_CONTENTS_DEPTH = 64
_COMMIT_FILE_STATUSES: dict[str, Literal["added", "modified", "deleted"]] = {
    "added": "added",
    "modified": "modified",
    "removed": "deleted",
    "deleted": "deleted",
}


_TERMINAL_SCAN_PAGES = 20


class GiteaProvider(GitProvider):
    """Opens pull requests through the Gitea REST API."""

    name = "gitea"
    capabilities = frozenset(
        {
            GitProviderCapability.MULTI_FILE_COMMIT,
            GitProviderCapability.FIND_PULL_REQUEST,
            GitProviderCapability.GET_REF_REVISION,
            GitProviderCapability.REPOSITORY_ARCHIVE,
            GitProviderCapability.COMPLETE_FILE_TREE,
            GitProviderCapability.LABELS,
            GitProviderCapability.REVIEWERS,
            GitProviderCapability.TEAM_REVIEWERS,
            GitProviderCapability.FIND_OPEN_PULL_REQUEST_FOR_BRANCH,
            GitProviderCapability.DELETE_PLAN_BRANCH,
            GitProviderCapability.COMMIT_ANCESTRY,
            GitProviderCapability.COMMIT_STATUS,
            GitProviderCapability.PULL_REQUEST_COMMENTS,
            GitProviderCapability.PULL_REQUEST_METADATA,
        }
    )

    def __init__(self, token: str, *, api_base_url: Optional[str] = None, **kwargs):
        base = api_base_url or "http://localhost:3000"
        if not base.endswith("/api/v1"):
            base = f"{base.rstrip('/')}/api/v1"
        super().__init__(token, api_base_url=base, **kwargs)

    def _headers(self) -> dict:
        return {"Accept": "application/json", "Authorization": f"token {self.token}"}

    async def get_default_branch(self, repo: GitRepository) -> str:
        response = await self._request("GET", f"/repos/{repo.owner}/{repo.name}")
        return response.json().get("default_branch") or repo.default_branch

    async def get_file(self, repo: GitRepository, path: str, ref: str) -> Optional[GitFile]:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/contents/{quote_git_path(path)}",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        data = response.json()
        if isinstance(data, list):
            raise GitProviderError(f"Path '{path}' is a directory, not a file.")
        try:
            content = base64.b64decode(data.get("content", "")).decode("utf-8")
        except Exception:
            content = data.get("content", "")
        return GitFile(path=path, content=content, sha=data.get("sha"), blob_sha=data.get("sha"))

    async def list_files(self, repo: GitRepository, ref: str) -> List[str]:
        files: List[str] = []
        page = 1
        seen_pages = set()
        first = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/trees/{quote(ref, safe='/')}",
            params={"recursive": "true", "per_page": "1000", "page": str(page)},
        )
        if first.status_code == 200:
            while True:
                if page in seen_pages or page > 10000:
                    raise GitProviderError("Gitea tree pagination did not progress; refusing partial results.")
                seen_pages.add(page)
                data = first.json()
                tree = data.get("tree", []) or []
                files.extend(item["path"] for item in tree if item.get("type") == "blob")
                if not data.get("truncated"):
                    return files
                page += 1
                first = await self._request(
                    "GET",
                    f"/repos/{repo.owner}/{repo.name}/git/trees/{quote(ref, safe='/')}",
                    params={"recursive": "true", "per_page": "1000", "page": str(page)},
                )
                if first.status_code != 200:
                    raise GitProviderError("Gitea tree pagination failed; refusing partial results.")

        # Older Gitea versions lack the trees API: walk all contents, failing
        # closed if any entry cannot be represented in the repository tree.
        return await self._list_files_via_contents(repo, ref, "", depth=0, files=[])

    async def get_tree(self, repo: GitRepository, ref: str) -> GitTree:
        """Read the recursive tree with object IDs (paginated; used only as a fallback)."""
        entries: dict[str, GitTreeEntry] = {}
        page = 1
        while page <= 10000:
            response = await self._request(
                "GET",
                f"/repos/{repo.owner}/{repo.name}/git/trees/{quote(ref, safe='/')}",
                params={"recursive": "true", "per_page": "1000", "page": str(page)},
            )
            if response.status_code != 200:
                if page == 1:
                    return await super().get_tree(repo, ref)
                raise GitProviderError("Gitea tree pagination failed; refusing partial results.")
            data = _git_response_object(response, provider=self.name, ref=ref)
            items = data.get("tree") or []
            if not isinstance(items, list):
                raise GitProviderError("Gitea returned an invalid repository tree; refusing partial results.")
            for item in items:
                if not isinstance(item, dict):
                    raise GitProviderError("Gitea returned an invalid tree entry; refusing partial results.")
                item_type = item.get("type")
                if item_type == "tree":
                    continue
                path, sha, mode = item.get("path"), item.get("sha"), item.get("mode")
                if (
                    item_type not in ("blob", "commit")
                    or not isinstance(path, str)
                    or not isinstance(sha, str)
                    or not sha
                    or path in entries
                ):
                    raise GitProviderError("Gitea returned an invalid tree entry; refusing partial results.")
                entries[path] = GitTreeEntry(
                    path=path,
                    sha=sha,
                    mode=mode if isinstance(mode, str) else "100644",
                    type=item_type,
                )
            if not data.get("truncated"):
                tree_sha = data.get("sha")
                return GitTree(entries=entries, sha=tree_sha if isinstance(tree_sha, str) else None)
            page += 1
        raise GitProviderError("Gitea tree pagination did not progress; refusing partial results.")

    async def compare_trees(self, repo: GitRepository, base_revision: str, head_ref: str) -> GitTreeComparison:
        """Diff a head that is the base or one commit on top of it with a single commit read.

        Any other shape (several commits, merges, older Gitea without affected
        file lists) falls back to a complete paginated tree comparison.
        """
        base_revision = _validate_full_commit_sha(base_revision, provider=self.name, ref=base_revision)
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/commits",
            params={"sha": head_ref, "limit": "1", "stat": "false", "verification": "false", "files": "true"},
        )
        if response.status_code == 404:
            raise GitProviderError(f"Gitea could not read {head_ref!r} in {repo.full_name}.", status_code=404)
        data = _git_response_data(response, provider=self.name, ref=head_ref)
        commit = data[0] if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None
        head_revision = commit.get("sha") if commit is not None else None
        if not is_full_commit_sha(head_revision):
            raise GitProviderError(f"Gitea returned an invalid head commit for {head_ref!r} in {repo.full_name}.")
        if head_revision.lower() == base_revision.lower():
            return GitTreeComparison(base_revision=base_revision, head_ref=head_ref, head_revision=head_revision)

        assert commit is not None
        parents = commit.get("parents")
        files = commit.get("files")
        parent_shas = (
            [parent.get("sha") for parent in parents if isinstance(parent, dict)] if isinstance(parents, list) else []
        )
        changes = self._single_commit_changes(files) if isinstance(files, list) else None
        if (
            changes is None
            or len(parent_shas) != 1
            or not isinstance(parent_shas[0], str)
            or parent_shas[0].lower() != base_revision.lower()
        ):
            comparison = await super().compare_trees(repo, base_revision, head_revision)
            return GitTreeComparison(
                base_revision=base_revision,
                head_ref=head_ref,
                changes=comparison.changes,
                head_revision=head_revision,
            )
        return GitTreeComparison(
            base_revision=base_revision,
            head_ref=head_ref,
            changes=changes,
            head_revision=head_revision,
        )

    @staticmethod
    def _single_commit_changes(files: list[object]) -> tuple[GitTreeChange, ...] | None:
        changes: dict[str, GitTreeChange] = {}
        for item in files:
            if not isinstance(item, dict):
                return None
            path = item.get("filename")
            status = _COMMIT_FILE_STATUSES.get(str(item.get("status")).casefold())
            if not isinstance(path, str) or status is None or path in changes:
                return None
            try:
                GitFileEdit(path=path, action="delete")
            except GitProviderError:
                return None
            changes[path] = GitTreeChange(path=path, status=status)
        return tuple(changes[path] for path in sorted(changes))

    async def is_ancestor(self, repo: GitRepository, ancestor_sha: str, descendant_sha: str) -> Optional[bool]:
        ancestor, descendant = self._validate_ancestry_request(repo, ancestor_sha, descendant_sha)
        if ancestor == descendant:
            return True
        # ``descendant...ancestor`` lists commits of the ancestor candidate that
        # the descendant lacks; none means the candidate is in its history.
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/compare/{descendant}...{ancestor}",
        )
        if response.status_code == 404:
            return None
        data = _git_response_object(response, provider=self.name, ref=f"{descendant}...{ancestor}")
        total = data.get("total_commits")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return None
        return total == 0

    async def get_archive(self, repo: GitRepository, revision: str, *, max_bytes: int) -> bytes:
        revision = self._validate_archive_request(repo, revision, max_bytes)
        return await self._request_archive(
            f"/repos/{quote(repo.owner, safe='/')}/{quote(repo.name, safe='')}/archive/"
            f"{quote(revision, safe='')}.tar.gz",
            max_bytes=max_bytes,
        )

    async def _list_files_via_contents(
        self,
        repo: GitRepository,
        ref: str,
        path: str,
        depth: int,
        files: List[str],
    ) -> List[str]:
        if depth > MAX_CONTENTS_DEPTH:
            raise GitProviderError("Gitea contents traversal exceeded the safety limit; refusing partial results.")
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/contents/{quote_git_path(path)}"
            if path
            else f"/repos/{repo.owner}/{repo.name}/contents",
            params={"ref": ref},
        )
        if response.status_code != 200:
            raise GitProviderError("Gitea contents traversal failed; refusing partial results.")
        data = response.json()
        if not isinstance(data, list):
            raise GitProviderError("Gitea returned an invalid contents listing; refusing partial results.")
        for item in data:
            if not isinstance(item, dict):
                raise GitProviderError("Gitea returned an invalid contents entry; refusing partial results.")
            item_path = item.get("path")
            item_type = item.get("type")
            if not isinstance(item_path, str) or not item_path:
                raise GitProviderError("Gitea returned a contents entry without a path; refusing partial results.")
            try:
                GitFileEdit(path=item_path, action="delete")
            except GitProviderError as exc:
                raise GitProviderError("Gitea returned an unsafe contents path; refusing partial results.") from exc
            if item_type == "file":
                if item_path in files:
                    raise GitProviderError("Gitea returned duplicate file paths; refusing partial results.")
                files.append(item_path)
                if len(files) > MAX_REPOSITORY_FILES:
                    raise GitProviderError("Gitea contents listing exceeded the file safety limit.")
            elif item_type == "dir":
                await self._list_files_via_contents(repo, ref, item_path, depth + 1, files)
            else:
                raise GitProviderError(
                    "Gitea contents listing contains an unsupported entry type; refusing partial results."
                )
        return sorted(files)

    async def create_branch(self, repo: GitRepository, branch: str, from_ref: str) -> None:
        branch = validate_git_ref(branch, field="branch name")
        from_ref = validate_git_ref(from_ref, field="base branch")
        # Create first: an existing branch answers 409, so the common case costs one write.
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/branches",
            json_body={"new_branch_name": branch, "old_branch_name": from_ref},
            ok_statuses=(200, 201, 409),
        )
        if response.status_code == 404:
            raise GitProviderError(f"Base branch '{from_ref}' not found in {repo.full_name}.", status_code=404)
        if response.status_code != 409:
            return
        exists = await self._request("GET", f"/repos/{repo.owner}/{repo.name}/branches/{quote(branch, safe='/')}")
        if exists.status_code == 200:
            logger.info("Branch %s already exists in %s; reusing it.", branch, repo.full_name)
            return
        raise GitProviderError(f"Gitea could not create branch '{branch}' in {repo.full_name}.", status_code=409)

    async def _branch_head_sha(self, repo: GitRepository, branch: str) -> str:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/branches/{quote(branch, safe='/')}",
            conflict_statuses=(404,),
        )
        data = _git_response_object(response, provider=self.name, ref=branch)
        commit = data.get("commit")
        head_sha = commit.get("id") if isinstance(commit, dict) else None
        if not isinstance(head_sha, str) or not head_sha:
            raise GitProviderError(f"Gitea returned an invalid branch revision for {repo.full_name}/{branch}.")
        return head_sha

    async def _resolve_tag_object(self, repo: GitRepository, git_object: object, ref: str, seen: set[str]) -> str:
        if not isinstance(git_object, dict):
            raise GitProviderError(f"Gitea returned an invalid tag response for ref '{ref}'.")
        object_type = git_object.get("type")
        object_sha = git_object.get("sha")
        if object_type == "commit":
            return _validate_full_commit_sha(object_sha, provider=self.name, ref=ref)
        if object_type != "tag":
            raise GitProviderError(f"Gitea returned an invalid tag target for ref '{ref}'.")

        tag_object_sha = _validate_full_commit_sha(object_sha, provider=self.name, ref=ref)
        if tag_object_sha in seen or len(seen) >= 32:
            raise GitProviderError(f"Gitea returned an invalid annotated tag chain for ref '{ref}'.")
        seen.add(tag_object_sha)
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/tags/{quote(tag_object_sha, safe='')}",
        )
        if response.status_code == 404:
            raise GitProviderError(f"Gitea returned a missing annotated tag object for ref '{ref}'.")
        tag_data = _git_response_object(response, provider=self.name, ref=ref)
        return await self._resolve_tag_object(repo, tag_data.get("object"), ref, seen)

    async def _tag_head_sha(self, repo: GitRepository, tag: str) -> str:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/git/refs/tags/{quote(tag, safe='/')}",
            conflict_statuses=(404,),
        )
        expected_ref = f"refs/tags/{tag}"
        data = _git_response_data(response, provider=self.name, ref=tag)
        if isinstance(data, list):
            matching_refs = [item for item in data if isinstance(item, dict) and item.get("ref") == expected_ref]
            if not matching_refs:
                raise GitProviderError(f"Gitea ref '{tag}' not found in {repo.full_name}.")
            if len(matching_refs) != 1:
                raise GitProviderError(f"Gitea returned an invalid tag response for ref '{tag}'.")
            data = matching_refs[0]
        if not isinstance(data, dict) or data.get("ref") != expected_ref:
            raise GitProviderError(f"Gitea returned an invalid tag response for ref '{tag}'.")
        return await self._resolve_tag_object(repo, data.get("object"), tag, set())

    async def get_ref_revision(self, repo: GitRepository, ref: str) -> str:
        ref = self._validate_ref_request(repo, ref)
        try:
            revision = await self._branch_head_sha(repo, ref)
        except GitConflictError:
            try:
                revision = await self._tag_head_sha(repo, ref)
            except GitConflictError as exc:
                raise GitProviderError(f"Gitea ref '{ref}' not found in {repo.full_name}.") from exc
        return _validate_full_commit_sha(revision, provider=self.name, ref=ref)

    async def _delete_staging_branch(self, repo: GitRepository, branch: str) -> None:
        try:
            await self._request(
                "DELETE",
                f"/repos/{repo.owner}/{repo.name}/branches/{quote(branch, safe='/')}",
                retryable=False,
            )
        except GitProviderError as exc:
            logger.warning("Could not remove temporary branch %s from %s: %s", branch, repo.full_name, exc)

    async def create_commit(self, request: GitCommitRequest) -> GitCommitResult:
        self._validate_commit_request(request)
        repo = request.repo
        branch = validate_git_ref(request.branch, field="branch name")
        base_revision = request.base_revision
        branch_path = f"/repos/{repo.owner}/{repo.name}/branches/{quote(branch, safe='/')}"
        current_head = await self._branch_head_sha(repo, branch)
        if current_head != base_revision:
            raise GitConflictError(f"Branch '{branch}' moved from base revision {base_revision} to {current_head}.")

        staging_branch = f"greenkube-commit-{uuid4().hex}"
        staging_branch_created = False
        try:
            file_states = await self._validate_commit_edits(request)
            files = []
            for edit in request.files:
                file_operation = {"operation": edit.action, "path": edit.path}
                if edit.action in ("create", "update"):
                    if edit.content is None:
                        raise GitProviderError(f"{edit.action.capitalize()} edits require file content.")
                    file_operation["content"] = base64.b64encode(edit.content.encode("utf-8")).decode("ascii")
                if edit.action in ("update", "delete"):
                    current = file_states[edit.path]
                    if current is None or not current.sha:
                        raise GitProviderError(
                            f"Gitea did not return a file SHA for '{edit.path}'; refusing the commit."
                        )
                    file_operation["sha"] = current.sha
                files.append(file_operation)

            # ``new_branch`` stages the commit on a throwaway branch cut from the
            # plan branch in the same call; the plan branch only moves through
            # the compare-and-swap update below once the parent is verified.
            staging_branch_created = True
            commit_response = await self._request(
                "POST",
                f"/repos/{repo.owner}/{repo.name}/contents",
                json_body={
                    "branch": branch,
                    "new_branch": staging_branch,
                    "message": request.message,
                    "author": {"name": request.author_name, "email": request.author_email},
                    "committer": {"name": request.author_name, "email": request.author_email},
                    "files": files,
                },
                conflict_statuses=(404, 409, 422),
                retryable=False,
            )
            response_data = commit_response.json()
            if not isinstance(response_data, dict):
                raise GitProviderError("Gitea returned an invalid atomic commit response.")
            commit_data = response_data.get("commit")
            if not isinstance(commit_data, dict):
                raise GitProviderError("Gitea returned an invalid atomic commit response.")
            commit_sha = commit_data.get("sha")
            if not isinstance(commit_sha, str) or not commit_sha:
                raise GitProviderError("Gitea returned an invalid commit SHA.")
            parents = commit_data.get("parents")
            if (
                not isinstance(parents, list)
                or len(parents) != 1
                or not isinstance(parents[0], dict)
                or parents[0].get("sha") != base_revision
            ):
                raise GitConflictError(
                    f"Gitea created commit {commit_sha} with an unexpected parent; expected {base_revision}."
                )

            await self._request(
                "PUT",
                branch_path,
                json_body={
                    "new_commit_id": commit_sha,
                    "old_commit_id": base_revision,
                    "force": False,
                },
                conflict_statuses=(404, 409, 422),
                retryable=False,
            )
            return GitCommitResult(
                commit_sha=commit_sha,
                branch=branch,
                files_changed=[edit.path for edit in request.files],
            )
        finally:
            if staging_branch_created:
                await self._delete_staging_branch(repo, staging_branch)

    async def _resolve_label_ids(self, repo: GitRepository, labels: tuple[str, ...]) -> list[int]:
        page = 1
        page_size = 50
        label_ids: dict[str, int] = {}
        while page <= 10_000:
            response = await self._request(
                "GET",
                f"/repos/{repo.owner}/{repo.name}/labels",
                params={"limit": str(page_size), "page": str(page)},
            )
            if response.status_code == 404:
                raise GitProviderError(f"Could not list labels in Gitea repository {repo.full_name}.")
            try:
                data = response.json()
            except ValueError as exc:
                raise GitProviderError("Gitea returned invalid JSON while listing repository labels.") from exc
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise GitProviderError("Gitea returned an invalid repository-label list.")
            for item in data:
                name = item.get("name")
                label_id = item.get("id")
                if (
                    not isinstance(name, str)
                    or isinstance(label_id, bool)
                    or not isinstance(label_id, int)
                    or label_id <= 0
                ):
                    raise GitProviderError("Gitea returned an invalid repository-label entry.")
                if name in labels:
                    previous_id = label_ids.get(name)
                    if previous_id is not None and previous_id != label_id:
                        raise GitProviderError(f"Gitea returned ambiguous repository labels named '{name}'.")
                    label_ids[name] = label_id
            next_page = pull_request_next_page(response, repo=repo, current_page=page)
            if next_page is None:
                if len(data) < page_size:
                    break
                next_page = page + 1
            page = next_page
        else:
            raise GitProviderError("Gitea repository-label pagination exceeded its safe limit.")

        for name in labels:
            if name not in label_ids:
                # Labels no longer have to exist beforehand (J2): create the missing ones.
                label_ids[name] = await self._create_label(repo, name)
        return [label_ids[name] for name in labels]

    async def _create_label(self, repo: GitRepository, name: str) -> int:
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/labels",
            json_body={"name": name, "color": _LABEL_COLOR, "description": "Created by GreenKube"},
            ok_statuses=(200, 201),
        )
        if response.status_code == 404:
            raise GitProviderError(f"Gitea repository {repo.full_name} does not allow creating label '{name}'.")
        data = _git_response_data(response, provider=self.name, ref=f"label {name}")
        label_id = data.get("id") if isinstance(data, dict) else None
        if isinstance(label_id, bool) or not isinstance(label_id, int) or label_id <= 0:
            raise GitProviderError(f"Gitea returned an invalid label after creating '{name}'.")
        return label_id

    async def add_pull_request_metadata(
        self,
        repo: GitRepository,
        pull_request: PullRequestInfo,
        *,
        labels: Sequence[str] = (),
        reviewers: Sequence[str] = (),
        team_reviewers: Sequence[str] = (),
    ) -> None:
        number = self._validate_pull_request_number(repo, pull_request.number)
        if labels:
            label_ids = await self._resolve_label_ids(repo, tuple(labels))
            response = await self._request(
                "POST",
                f"/repos/{repo.owner}/{repo.name}/issues/{number}/labels",
                json_body={"labels": label_ids},
            )
            if response.status_code == 404:
                raise GitProviderError(f"Gitea could not label pull request {number} in {repo.full_name}.")
        if reviewers or team_reviewers:
            response = await self._request(
                "POST",
                f"/repos/{repo.owner}/{repo.name}/pulls/{number}/requested_reviewers",
                json_body={"reviewers": list(reviewers), "team_reviewers": list(team_reviewers)},
            )
            if response.status_code == 404:
                raise GitProviderError(f"Gitea could not request reviewers on pull request {number}.")

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
        payload: dict[str, str] = {"state": state, "context": context, "description": description[:255]}
        if target_url:
            payload["target_url"] = target_url
        response = await self._request(
            "POST", f"/repos/{repo.owner}/{repo.name}/statuses/{quote(sha, safe='')}", json_body=payload
        )
        if response.status_code == 404:
            raise GitProviderError(f"Gitea could not set a commit status on {sha} in {repo.full_name}.")

    async def _list_pull_request_comments(
        self, repo: GitRepository, number: int, page: int
    ) -> tuple[list[PullRequestComment], bool]:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
            params={"page": str(page), "limit": str(COMMENT_PAGE_SIZE)},
        )
        if response.status_code == 404:
            raise GitProviderError(f"Gitea could not list comments of pull request {number}.")
        comments = parse_comment_list(response, provider=self.name)
        # Older Gitea versions ignore paging and return every comment at once.
        has_next = pull_request_next_page(response, repo=repo, current_page=page) is not None
        return comments, has_next

    async def _create_pull_request_comment(self, repo: GitRepository, number: int, body: str) -> int:
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/issues/{number}/comments",
            json_body={"body": body},
            retryable=False,
        )
        return parse_comment_id(response, provider=self.name)

    async def _update_pull_request_comment(self, repo: GitRepository, number: int, comment_id: int, body: str) -> None:
        response = await self._request(
            "PATCH",
            f"/repos/{repo.owner}/{repo.name}/issues/comments/{comment_id}",
            json_body={"body": body},
        )
        if response.status_code == 404:
            raise GitProviderError(f"Gitea comment {comment_id} disappeared before it could be updated.")

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
        request_body: dict[str, object] = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
        }
        if selected_options.labels:
            request_body["labels"] = await self._resolve_label_ids(repo, selected_options.labels)
        if selected_options.reviewers:
            request_body["reviewers"] = list(selected_options.reviewers)
        if selected_options.team_reviewers:
            request_body["team_reviewers"] = list(selected_options.team_reviewers)
        response = await self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/pulls",
            json_body=request_body,
        )
        if response.status_code == 404:
            raise GitProviderError(
                f"Could not open a pull request in {repo.full_name}: repository or branch not found."
            )
        return normalize_pull_request(response.json(), repo=repo)

    def _open_pull_requests_query(self, repo: GitRepository, base: str, limit: int) -> tuple[str, dict[str, str]]:
        # Gitea has no base-branch filter: the base is checked on every item.
        return (f"/repos/{repo.owner}/{repo.name}/pulls", {"state": "open", "sort": "newest", "limit": str(limit)})

    def _pull_request_commits_path(self, repo: GitRepository, number: int) -> str:
        return f"/repos/{repo.owner}/{repo.name}/pulls/{number}/commits"

    async def get_pull_request(self, repo: GitRepository, number: int) -> PullRequestInfo:
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise GitProviderError("Pull-request number must be a positive integer.")
        response = await self._request("GET", f"/repos/{repo.owner}/{repo.name}/pulls/{number}")
        if response.status_code == 404:
            raise GitProviderError(f"Pull request {number} not found in {repo.full_name}.")
        return normalize_pull_request(response.json(), repo=repo)

    async def find_terminal_pull_request(self, repo: GitRepository, head: str, base: str) -> Optional[PullRequestInfo]:
        """The most recently updated merged or closed request from ``head`` into ``base`` (D10).

        Gitea has no head filter on the list endpoint: closed requests are scanned
        most recently updated first, bounded to the newest ``_TERMINAL_SCAN_PAGES`` pages
        (a request merged while a lease was lost is among the most recently updated).
        """
        if repo.provider != self.name:
            raise GitProviderError(f"Pull-request lookup targets provider '{repo.provider}', not '{self.name}'.")
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        page_size = 50
        page = 1
        while page <= _TERMINAL_SCAN_PAGES:
            response = await self._request(
                "GET",
                f"/repos/{repo.owner}/{repo.name}/pulls",
                params={"state": "closed", "sort": "recentupdate", "limit": str(page_size), "page": str(page)},
            )
            if response.status_code == 404:
                raise GitProviderError(f"Could not list pull requests in {repo.full_name}: repository not found.")
            data = parse_pull_request_list(response, repo=repo)
            for item in data:
                pull_request_head = item.get("head")
                pull_request_base = item.get("base")
                match = normalize_terminal_pull_request_match(
                    item,
                    repo=repo,
                    head=head,
                    base=base,
                    provider_head=pull_request_head.get("ref") if isinstance(pull_request_head, dict) else None,
                    provider_base=pull_request_base.get("ref") if isinstance(pull_request_base, dict) else None,
                    open_states=("open",),
                )
                if match is not None:
                    return match
            next_page = pull_request_next_page(response, repo=repo, current_page=page)
            if next_page is not None:
                page = next_page
            elif len(data) < page_size:
                return None
            else:
                page += 1
        return None

    async def find_pull_request(self, repo: GitRepository, head: str, base: str) -> Optional[PullRequestInfo]:
        if repo.provider != self.name:
            raise GitProviderError(f"Pull-request lookup targets provider '{repo.provider}', not '{self.name}'.")
        head = validate_git_ref(head, field="branch name")
        base = validate_git_ref(base, field="base branch")
        page_size = 50
        page = 1
        while page <= 10000:
            response = await self._request(
                "GET",
                f"/repos/{repo.owner}/{repo.name}/pulls",
                params={"state": "open", "limit": str(page_size), "page": str(page)},
            )
            if response.status_code == 404:
                raise GitProviderError(f"Could not list pull requests in {repo.full_name}: repository not found.")
            data = parse_pull_request_list(response, repo=repo)
            for item in data:
                pull_request_head = item.get("head")
                pull_request_base = item.get("base")
                match = normalize_open_pull_request_match(
                    item,
                    repo=repo,
                    head=head,
                    base=base,
                    provider_head=pull_request_head.get("ref") if isinstance(pull_request_head, dict) else None,
                    provider_base=pull_request_base.get("ref") if isinstance(pull_request_base, dict) else None,
                    open_states=("open",),
                )
                if match is not None:
                    return match
            next_page = pull_request_next_page(response, repo=repo, current_page=page)
            if next_page is not None:
                page = next_page
            elif len(data) < page_size:
                return None
            else:
                page += 1
        raise GitProviderError("Gitea pull-request pagination exceeded the safety limit.")

    async def find_open_pull_request_for_branch(
        self,
        repo: GitRepository,
        branch: str,
    ) -> Optional[PullRequestInfo]:
        branch = self._validate_plan_branch_request(repo, branch)
        page_size = 50
        page = 1
        while page <= 10000:
            response = await self._request(
                "GET",
                f"/repos/{repo.owner}/{repo.name}/pulls",
                params={"state": "open", "limit": str(page_size), "page": str(page)},
            )
            if response.status_code == 404:
                raise GitProviderError(f"Could not list pull requests in {repo.full_name}: repository not found.")
            data = parse_pull_request_list(response, repo=repo)
            for item in data:
                pull_request_head = item.get("head")
                pull_request_base = item.get("base")
                match = normalize_open_pull_request_branch_match(
                    item,
                    repo=repo,
                    branch=branch,
                    provider_head=pull_request_head.get("ref") if isinstance(pull_request_head, dict) else None,
                    provider_base=pull_request_base.get("ref") if isinstance(pull_request_base, dict) else None,
                    open_states=("open",),
                )
                if match is not None:
                    return match
            next_page = pull_request_next_page(response, repo=repo, current_page=page)
            if next_page is not None:
                page = next_page
            elif len(data) < page_size:
                return None
            else:
                page += 1
        raise GitProviderError("Gitea pull-request pagination exceeded the safety limit.")

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
            f"/repos/{repo.owner}/{repo.name}/branches/{quote(branch, safe='/')}",
        )
        if response.status_code == 404:
            return
