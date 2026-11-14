"""In-memory Git hosting fakes that speak the GitHub, GitLab and Gitea REST APIs.

They back the real provider adapters through ``respx`` so tests can count every
HTTP request an operation makes against a repository of any size.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from greenkube.automation.git.base import git_blob_id

#: The user ID of GreenKube's token on every fake server.
BOT_USER_ID = 4242


@dataclass
class FakeCommit:
    sha: str
    tree: dict[str, tuple[str, str]]  # path -> (mode, blob sha)
    parents: list[str]
    message: str


@dataclass
class FakeGitRepository:
    """A tiny Git object model: blobs, flat trees, commits and branch refs."""

    blobs: dict[str, bytes] = field(default_factory=dict)
    commits: dict[str, FakeCommit] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    _counter: int = 0

    @classmethod
    def with_files(cls, files: dict[str, str], *, branch: str = "main") -> FakeGitRepository:
        repository = cls()
        tree = {path: ("100644", repository.put_blob(content)) for path, content in files.items()}
        repository.refs[branch] = repository.put_commit(tree, [], "initial")
        return repository

    def put_blob(self, content: str | bytes) -> str:
        payload = content.encode() if isinstance(content, str) else content
        sha = git_blob_id(payload)
        self.blobs[sha] = payload
        return sha

    def put_commit(self, tree: dict[str, tuple[str, str]], parents: list[str], message: str) -> str:
        self._counter += 1
        material = json.dumps([sorted(tree.items()), parents, message, self._counter]).encode()
        sha = hashlib.sha1(b"commit " + material).hexdigest()
        self.commits[sha] = FakeCommit(sha=sha, tree=dict(tree), parents=list(parents), message=message)
        return sha

    def tree_sha(self, tree: dict[str, tuple[str, str]]) -> str:
        return hashlib.sha1(b"tree " + json.dumps(sorted(tree.items())).encode()).hexdigest()

    def resolve(self, ref: str) -> str | None:
        if ref in self.refs:
            return self.refs[ref]
        return ref if ref in self.commits else None

    def tree_of(self, ref: str) -> dict[str, tuple[str, str]] | None:
        commit = self.resolve(ref)
        return self.commits[commit].tree if commit is not None else None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        pending, seen = [descendant], set()
        while pending:
            current = pending.pop()
            if current == ancestor:
                return True
            if current in seen or current not in self.commits:
                continue
            seen.add(current)
            pending.extend(self.commits[current].parents)
        return False

    def commit_on(self, branch: str, changes: dict[str, str | None], message: str = "change") -> str:
        """Commit file changes (``None`` deletes) on top of ``branch`` and advance it."""
        parent = self.refs[branch]
        tree = dict(self.commits[parent].tree)
        for path, content in changes.items():
            if content is None:
                tree.pop(path, None)
            else:
                tree[path] = ("100644", self.put_blob(content))
        sha = self.put_commit(tree, [parent], message)
        self.refs[branch] = sha
        return sha

    def file_text(self, ref: str, path: str) -> str | None:
        tree = self.tree_of(ref)
        if tree is None or path not in tree:
            return None
        return self.blobs[tree[path][1]].decode()


def _json(status: int, payload: object) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


class _RecordingServer:
    base_url: str

    def __init__(self, repository: FakeGitRepository) -> None:
        self.repository = repository
        self.requests: list[str] = []
        self.pull_requests: list[dict] = []
        # Collaboration state (labels, reviewers, statuses, comments; roadmap J2-J5).
        self.labels: dict[str, int] = {}
        self.pull_request_labels: dict[int, list[str]] = {}
        self.requested_reviewers: dict[int, list[str]] = {}
        self.statuses: list[dict] = []
        self.comments: dict[int, dict] = {}
        self.users: dict[str, int] = {}
        # The token's own user: ``GET /user`` answers ``identity_status`` with it.
        self.identity = {"id": BOT_USER_ID, "login": "greenkube-bot", "username": "greenkube-bot"}
        self.identity_status = 200

    #: Path of the "authenticated user" endpoint and the comment/PR author key.
    identity_path = "/user"
    author_key = "user"

    def _author(self, user_id: int) -> dict:
        return {"id": user_id, "login": f"user-{user_id}", "username": f"user-{user_id}"}

    def add_comment(self, comment_id: int, number: int, body: str, *, author_id: int) -> None:
        """Seed a comment written by someone else (``author_id``)."""
        self.comments[comment_id] = {"id": comment_id, "number": number, "body": body, "author_id": author_id}

    def _comment(self, number: int, body: str) -> dict:
        comment_id = 1000 + len(self.comments)
        self.comments[comment_id] = {"id": comment_id, "number": number, "body": body, "author_id": BOT_USER_ID}
        return {"id": comment_id, "body": body, self.author_key: self._author(BOT_USER_ID)}

    def _comments_of(self, number: int) -> list[dict]:
        return [
            {"id": item["id"], "body": item["body"], self.author_key: self._author(item["author_id"])}
            for item in self.comments.values()
            if item["number"] == number
        ]

    def single_pull_request(self, number: int) -> httpx.Response:
        if not 1 <= number <= len(self.pull_requests):
            return _json(404, {"message": "Not Found"})
        return _json(200, self.pull_requests[number - 1])

    def collaboration(self, method: str, local: str, body: dict, *, issues: str) -> httpx.Response | None:
        """Shared GitHub/Gitea-style labels, statuses, reviewers and issue comments."""
        parts = local.strip("/").split("/")
        if method == "POST" and len(parts) == 2 and parts[0] == "statuses":
            self.statuses.append({"sha": parts[1], **body})
            return _json(201, {"id": len(self.statuses), **body})
        if method == "GET" and len(parts) == 2 and parts[0] == "pulls" and parts[1].isdigit():
            return self.single_pull_request(int(parts[1]))
        if len(parts) == 3 and parts[0] == "issues" and parts[2] == "labels" and method == "POST":
            names = [
                name if isinstance(name, str) else next(key for key, value in self.labels.items() if value == name)
                for name in body["labels"]
            ]
            current = self.pull_request_labels.setdefault(int(parts[1]), [])
            current.extend(name for name in names if name not in current)
            return _json(200, [{"name": name} for name in current])
        if len(parts) == 3 and parts[0] == "pulls" and parts[2] == "requested_reviewers":
            number = int(parts[1])
            current = self.requested_reviewers.setdefault(number, [])
            if method == "GET":
                return _json(200, {"users": [{"login": name} for name in current], "teams": []})
            current.extend(name for name in [*body["reviewers"], *body["team_reviewers"]] if name not in current)
            return _json(201, {"number": number})
        if len(parts) == 3 and parts[0] == "issues" and parts[2] == "comments":
            number = int(parts[1])
            if method == "GET":
                return _json(200, self._comments_of(number))
            if method == "POST":
                return _json(201, self._comment(number, body["body"]))
        if method == "PATCH" and len(parts) == 3 and parts[:2] == [issues, "comments"]:
            comment = self.comments.get(int(parts[2]))
            if comment is None:
                return _json(404, {"message": "Not Found"})
            comment["body"] = body["body"]
            return _json(200, {"id": comment["id"], "body": comment["body"]})
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?", 1)[0]
        self.requests.append(f"{request.method} {unquote(path)}")
        query = {key: values for key, values in parse_qs(urlsplit(str(request.url)).query).items()}
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and path == self.identity_path:
            payload = self.identity if self.identity_status == 200 else {"message": "Forbidden"}
            return _json(self.identity_status, payload)
        response = self.route(request.method, path, query, body)
        if response is None:
            raise AssertionError(f"unexpected provider request: {request.method} {request.url}")
        return response

    def route(self, method: str, path: str, query: dict, body: dict) -> httpx.Response | None:
        raise NotImplementedError

    def calls(self) -> int:
        return len(self.requests)

    def breakdown(self) -> Counter[str]:
        return Counter(self.requests)


class FakeGitHubServer(_RecordingServer):
    base_url = "https://api.github.com"

    def __init__(self, repository: FakeGitRepository, *, owner: str = "acme", name: str = "gitops") -> None:
        super().__init__(repository)
        self.prefix = f"/repos/{owner}/{name}"
        self.html = f"https://github.com/{owner}/{name}"

    def _tree_payload(self, tree: dict[str, tuple[str, str]]) -> dict:
        directories = sorted(
            {"/".join(path.split("/")[:index]) for path in tree for index in range(1, path.count("/") + 1)}
        )
        entries: list[dict[str, object]] = [
            {"path": directory, "mode": "040000", "type": "tree", "sha": "0" * 40} for directory in directories
        ]
        entries.extend(
            {"path": path, "mode": mode, "type": "blob", "sha": sha, "size": len(self.repository.blobs[sha])}
            for path, (mode, sha) in sorted(tree.items())
        )
        return {"sha": self.repository.tree_sha(tree), "truncated": False, "tree": entries}

    def route(self, method: str, path: str, query: dict, body: dict) -> httpx.Response | None:
        repository = self.repository
        if not path.startswith(self.prefix):
            return None
        local = unquote(path[len(self.prefix) :])
        if method == "GET" and local.startswith("/git/ref/heads/"):
            sha = repository.refs.get(local[len("/git/ref/heads/") :])
            if sha is None:
                return _json(404, {"message": "Not Found"})
            return _json(200, {"object": {"sha": sha, "type": "commit"}})
        if method == "POST" and local == "/git/refs":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in repository.refs:
                return _json(422, {"message": "Reference already exists"})
            repository.refs[branch] = body["sha"]
            return _json(201, {"ref": body["ref"], "object": {"sha": body["sha"]}})
        if method == "PATCH" and local.startswith("/git/refs/heads/"):
            branch = local[len("/git/refs/heads/") :]
            current = repository.refs.get(branch)
            if current is None:
                return _json(404, {"message": "Not Found"})
            if not body.get("force") and not repository.is_ancestor(current, body["sha"]):
                return _json(422, {"message": "Update is not a fast forward"})
            repository.refs[branch] = body["sha"]
            return _json(200, {"object": {"sha": body["sha"]}})
        if method == "GET" and local.startswith("/git/trees/"):
            ref = local[len("/git/trees/") :]
            tree = repository.tree_of(ref)
            if tree is None:
                tree = next(
                    (commit.tree for commit in repository.commits.values() if repository.tree_sha(commit.tree) == ref),
                    None,
                )
            if tree is None:
                return _json(404, {"message": "Not Found"})
            return _json(200, self._tree_payload(tree))
        if method == "POST" and local == "/git/trees":
            base = next(
                commit.tree
                for commit in repository.commits.values()
                if repository.tree_sha(commit.tree) == body["base_tree"]
            )
            tree = dict(base)
            for entry in body["tree"]:
                if entry.get("sha", "") is None:
                    tree.pop(entry["path"], None)
                else:
                    tree[entry["path"]] = (entry["mode"], repository.put_blob(entry["content"]))
            self._pending_trees = getattr(self, "_pending_trees", {})
            sha = repository.tree_sha(tree)
            self._pending_trees[sha] = tree
            return _json(201, {"sha": sha})
        if method == "POST" and local == "/git/commits":
            tree = self._pending_trees[body["tree"]]
            sha = repository.put_commit(tree, body["parents"], body["message"])
            return _json(201, {"sha": sha})
        if method == "GET" and local.startswith("/contents/"):
            tree = repository.tree_of(query.get("ref", ["main"])[0])
            file_path = local[len("/contents/") :]
            if tree is None or file_path not in tree:
                return _json(404, {"message": "Not Found"})
            sha = tree[file_path][1]
            return _json(200, {"type": "file", "path": file_path, "sha": sha, "content": _b64(repository.blobs[sha])})
        if method == "GET" and local.startswith("/compare/"):
            base, head = local[len("/compare/") :].split("...", 1)
            if base not in repository.commits or head not in repository.commits:
                return _json(404, {"message": "Not Found"})
            if base == head:
                status = "identical"
            elif repository.is_ancestor(base, head):
                status = "ahead"
            elif repository.is_ancestor(head, base):
                status = "behind"
            else:
                status = "diverged"
            return _json(
                200, {"status": status, "ahead_by": 0, "behind_by": 0 if status in ("ahead", "identical") else 1}
            )
        if method == "GET" and local == "/pulls":
            head = query.get("head", [None])[0]
            base = query.get("base", [None])[0]
            state = query.get("state", ["open"])[0]
            matches = [
                pull_request
                for pull_request in self.pull_requests
                if state in ("all", pull_request["state"])
                and (head is None or head.split(":", 1)[-1] == pull_request["head"]["ref"])
                and (base is None or base == pull_request["base"]["ref"])
            ]
            return _json(200, matches)
        if method == "POST" and local == "/pulls":
            if any(
                pull_request["head"]["ref"] == body["head"] and pull_request["state"] == "open"
                for pull_request in self.pull_requests
            ):
                return _json(422, {"message": "A pull request already exists"})
            number = len(self.pull_requests) + 1
            pull_request = {
                "number": number,
                "html_url": f"{self.html}/pull/{number}",
                "state": "open",
                "draft": bool(body.get("draft")),
                "merged": False,
                "head": {"ref": body["head"], "sha": repository.refs[body["head"]]},
                "base": {"ref": body["base"]},
                "user": self._author(BOT_USER_ID),
            }
            self.pull_requests.append(pull_request)
            return _json(201, pull_request)
        return self.collaboration(method, local, body, issues="issues")


class FakeGitLabServer(_RecordingServer):
    base_url = "https://gitlab.com"
    identity_path = "/api/v4/user"
    author_key = "author"

    def __init__(self, repository: FakeGitRepository, *, project: str = "acme/gitops") -> None:
        super().__init__(repository)
        self.prefix = "/api/v4/projects/" + project.replace("/", "%2F")
        self.html = f"https://gitlab.com/{project}"

    def route(self, method: str, path: str, query: dict, body: dict) -> httpx.Response | None:
        repository = self.repository
        if method == "GET" and path == "/api/v4/users":
            username = query.get("username", [""])[0]
            user_id = self.users.get(username)
            return _json(200, [] if user_id is None else [{"id": user_id, "username": username}])
        if not path.startswith(self.prefix):
            return None
        local = path[len(self.prefix) :]
        if method == "GET" and local.startswith("/repository/branches/"):
            sha = repository.refs.get(unquote(local[len("/repository/branches/") :]))
            if sha is None:
                return _json(404, {"message": "404 Branch Not Found"})
            return _json(200, {"commit": {"id": sha}})
        if method == "POST" and local == "/repository/branches":
            branch, ref = query["branch"][0], query["ref"][0]
            if branch in repository.refs:
                return _json(400, {"message": "Branch already exists"})
            repository.refs[branch] = repository.resolve(ref) or ""
            return _json(201, {"name": branch, "commit": {"id": repository.refs[branch]}})
        if method == "GET" and local == "/repository/compare":
            base, head = query["from"][0], query["to"][0]
            base_tree, head_tree = repository.tree_of(base), repository.tree_of(head)
            if base_tree is None or head_tree is None:
                return _json(404, {"message": "404 Not Found"})
            diffs = []
            for file_path in sorted(set(base_tree) | set(head_tree)):
                if base_tree.get(file_path) == head_tree.get(file_path):
                    continue
                diffs.append(
                    {
                        "old_path": file_path,
                        "new_path": file_path,
                        "new_file": file_path not in base_tree,
                        "deleted_file": file_path not in head_tree,
                        "renamed_file": False,
                        "diff": "@@ -1 +1 @@\n",
                    }
                )
            head_sha = repository.resolve(head)
            return _json(
                200,
                {
                    "commit": None if head_sha == base else {"id": head_sha},
                    "commits": [],
                    "diffs": diffs,
                    "compare_timeout": False,
                    "compare_same_ref": head_sha == base,
                },
            )
        if method == "GET" and local.startswith("/repository/files/"):
            file_path = unquote(local[len("/repository/files/") :])
            ref = query["ref"][0]
            tree = repository.tree_of(ref)
            if tree is None or file_path not in tree:
                return _json(404, {"message": "404 File Not Found"})
            sha = tree[file_path][1]
            return _json(
                200,
                {
                    "file_path": file_path,
                    "encoding": "base64",
                    "content": _b64(repository.blobs[sha]),
                    "blob_id": sha,
                    "last_commit_id": repository.resolve(ref),
                },
            )
        if method == "POST" and local == "/repository/commits":
            branch = body["branch"]
            if repository.refs.get(branch) != body["start_sha"]:
                return _json(400, {"message": "start_sha has changed since"})
            changes = {
                action["file_path"]: (None if action["action"] == "delete" else action["content"])
                for action in body["actions"]
            }
            sha = repository.commit_on(branch, changes, body["commit_message"])
            return _json(201, {"id": sha, "parent_ids": [body["start_sha"]]})
        if method == "GET" and local == "/repository/merge_base":
            first, second = query["refs[]"]
            for candidate in (first, second):
                other = second if candidate == first else first
                if repository.is_ancestor(candidate, other):
                    return _json(200, {"id": candidate})
            return _json(404, {"message": "404 Merge Base Not Found"})
        if method == "GET" and local == "/merge_requests":
            source = query.get("source_branch", [None])[0]
            target = query.get("target_branch", [None])[0]
            state = query.get("state", ["all"])[0]
            matches = [
                merge_request
                for merge_request in self.pull_requests
                if state in ("all", merge_request["state"])
                and (source is None or merge_request["source_branch"] == source)
                and (target is None or merge_request["target_branch"] == target)
            ]
            return _json(200, matches)
        if method == "POST" and local == "/merge_requests":
            number = len(self.pull_requests) + 1
            merge_request = {
                "iid": number,
                "web_url": f"{self.html}/-/merge_requests/{number}",
                "state": "opened",
                "source_branch": body["source_branch"],
                "target_branch": body["target_branch"],
                "sha": repository.refs[body["source_branch"]],
                "reviewers": [],
                "author": self._author(BOT_USER_ID),
            }
            self.pull_requests.append(merge_request)
            return _json(201, merge_request)
        parts = local.strip("/").split("/")
        if method == "POST" and len(parts) == 2 and parts[0] == "statuses":
            self.statuses.append({"sha": parts[1], **body})
            return _json(201, {"id": len(self.statuses), **body})
        if method == "GET" and len(parts) == 2 and parts[0] == "merge_requests" and parts[1].isdigit():
            return self.single_pull_request(int(parts[1]))
        if method == "PUT" and len(parts) == 2 and parts[0] == "merge_requests":
            merge_request = self.pull_requests[int(parts[1]) - 1]
            if body.get("add_labels"):
                current = self.pull_request_labels.setdefault(int(parts[1]), [])
                current.extend(name for name in body["add_labels"].split(",") if name not in current)
            if "reviewer_ids" in body:
                merge_request["reviewers"] = [{"id": item} for item in body["reviewer_ids"]]
            return _json(200, merge_request)
        if len(parts) == 3 and parts[0] == "merge_requests" and parts[2] == "notes":
            if method == "GET":
                return _json(200, self._comments_of(int(parts[1])))
            if method == "POST":
                return _json(201, self._comment(int(parts[1]), body["body"]))
        if method == "PUT" and len(parts) == 4 and parts[0] == "merge_requests" and parts[2] == "notes":
            comment = self.comments.get(int(parts[3]))
            if comment is None:
                return _json(404, {"message": "404 Not found"})
            comment["body"] = body["body"]
            return _json(200, {"id": comment["id"], "body": comment["body"]})
        return None


class FakeGiteaServer(_RecordingServer):
    base_url = "http://gitea.local"
    identity_path = "/api/v1/user"

    def __init__(self, repository: FakeGitRepository, *, owner: str = "acme", name: str = "gitops") -> None:
        super().__init__(repository)
        self.prefix = f"/api/v1/repos/{owner}/{name}"
        self.html = f"http://gitea.local/{owner}/{name}"
        self.supports_old_ref_name = True

    def route(self, method: str, path: str, query: dict, body: dict) -> httpx.Response | None:
        repository = self.repository
        if not path.startswith(self.prefix):
            return None
        local = unquote(path[len(self.prefix) :])
        if method == "GET" and local.startswith("/branches/"):
            sha = repository.refs.get(local[len("/branches/") :])
            if sha is None:
                return _json(404, {"message": "branch not found"})
            return _json(200, {"commit": {"id": sha}})
        if method == "POST" and local == "/branches":
            branch = body["new_branch_name"]
            if branch in repository.refs:
                return _json(409, {"message": "The branch already exists."})
            # Gitea 1.21+ creates a branch at ``old_ref_name`` (a commit); an older Gitea ignores
            # it and branches from ``old_branch_name`` or the default branch.
            requested = body.get("old_ref_name") if self.supports_old_ref_name else None
            source = repository.resolve(requested or body.get("old_branch_name") or "main")
            if source is None:
                return _json(404, {"message": "old branch not found"})
            repository.refs[branch] = source
            return _json(201, {"name": branch, "commit": {"id": source}})
        if method == "PUT" and local.startswith("/branches/"):
            branch = local[len("/branches/") :]
            if repository.refs.get(branch) != body["old_commit_id"]:
                return _json(409, {"message": "branch tip changed"})
            repository.refs[branch] = body["new_commit_id"]
            return httpx.Response(204)
        if method == "DELETE" and local.startswith("/branches/"):
            repository.refs.pop(local[len("/branches/") :], None)
            return httpx.Response(204)
        if method == "GET" and local == "/commits":
            sha = repository.resolve(query["sha"][0])
            if sha is None:
                return _json(404, {"message": "not found"})
            commit = repository.commits[sha]
            parent_tree: dict[str, tuple[str, str]] = (
                repository.commits[commit.parents[0]].tree if commit.parents else {}
            )
            files = []
            for file_path in sorted(set(parent_tree) | set(commit.tree)):
                if parent_tree.get(file_path) == commit.tree.get(file_path):
                    continue
                status = (
                    "added"
                    if file_path not in parent_tree
                    else "removed"
                    if file_path not in commit.tree
                    else "modified"
                )
                files.append({"filename": file_path, "status": status})
            return _json(200, [{"sha": sha, "parents": [{"sha": parent} for parent in commit.parents], "files": files}])
        if method == "GET" and local.startswith("/contents/"):
            tree = repository.tree_of(query.get("ref", ["main"])[0])
            file_path = local[len("/contents/") :]
            if tree is None or file_path not in tree:
                return _json(404, {"message": "not found"})
            sha = tree[file_path][1]
            return _json(200, {"type": "file", "path": file_path, "sha": sha, "content": _b64(repository.blobs[sha])})
        if method == "POST" and local == "/contents":
            parent = repository.refs[body["branch"]]
            new_branch = body.get("new_branch") or body["branch"]
            if new_branch != body["branch"]:
                if new_branch in repository.refs:
                    return _json(409, {"message": "branch already exists"})
                repository.refs[new_branch] = parent
            changes = {
                item["path"]: (None if item["operation"] == "delete" else base64.b64decode(item["content"]).decode())
                for item in body["files"]
            }
            sha = repository.commit_on(new_branch, changes, body["message"])
            return _json(201, {"commit": {"sha": sha, "parents": [{"sha": parent}]}})
        if method == "GET" and local.startswith("/compare/"):
            base, head = local[len("/compare/") :].split("...", 1)
            if base not in repository.commits or head not in repository.commits:
                return _json(404, {"message": "not found"})
            missing = 0 if repository.is_ancestor(head, base) else 1
            return _json(200, {"total_commits": missing, "commits": []})
        if method == "GET" and local == "/pulls":
            state = query.get("state", ["open"])[0]
            return _json(
                200, [pull_request for pull_request in self.pull_requests if state in ("all", pull_request["state"])]
            )
        if method == "POST" and local == "/pulls":
            number = len(self.pull_requests) + 1
            pull_request = {
                "number": number,
                "html_url": f"{self.html}/pulls/{number}",
                "state": "open",
                "merged": False,
                "head": {"ref": body["head"], "sha": repository.refs[body["head"]]},
                "base": {"ref": body["base"]},
                "user": self._author(BOT_USER_ID),
            }
            self.pull_requests.append(pull_request)
            return _json(201, pull_request)
        if method == "GET" and local == "/labels":
            return _json(200, [{"id": label_id, "name": name} for name, label_id in self.labels.items()])
        if method == "POST" and local == "/labels":
            self.labels[body["name"]] = 500 + len(self.labels)
            return _json(201, {"id": self.labels[body["name"]], "name": body["name"]})
        return self.collaboration(method, local, body, issues="issues")
