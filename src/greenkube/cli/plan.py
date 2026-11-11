# src/greenkube/cli/plan.py
"""``greenkube plan``: deliver approved plans without Git credentials in the cluster (roadmap F4).

``patch`` mode::

    export GREENKUBE_API_URL=https://greenkube.example.com GREENKUBE_API_KEY=gk_...
    cd my-gitops-repo
    greenkube plan apply-patch pln-0123 --push
    # open the pull request from the printed branch, then (optional, the branch name already links it):
    greenkube plan report-delivery pln-0123 --pr-url https://github.com/acme/gitops/pull/42

``apply-patch`` downloads the approved patch, creates the deterministic plan
branch on the plan's base revision and applies it with ``git am --3way`` (the
commit keeps the ``GreenKube-Plan`` trailer). It refuses a dirty working tree
and never force-pushes. ``report-delivery`` is the "I opened PR <url>" link-back
(``plans:deliver`` scope); a CI job uses the same call with ``--head-sha``.

The API key is read from ``GREENKUBE_API_KEY`` (never from a command-line
argument, which other users could read from the process list).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Deliver approved GitOps plans yourself (patch and ci delivery modes).")

_TIMEOUT_SECONDS = 30.0


class PlanCliError(RuntimeError):
    """A failure the command reports without a traceback."""


def _api(api_url: Optional[str]) -> tuple[str, dict[str, str]]:
    base = (api_url or os.getenv("GREENKUBE_API_URL") or "").strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise PlanCliError("Set --api-url or GREENKUBE_API_URL to the GreenKube URL (https://...).")
    headers = {"Accept": "application/json"}
    key = (os.getenv("GREENKUBE_API_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return base, headers


def download_patch(plan_id: str, *, api_url: Optional[str] = None, client=None) -> tuple[str, dict[str, str]]:
    """The approved plan's patch and its ``X-GreenKube-*`` headers (branch, base branch, base revision)."""
    import httpx

    base, headers = _api(api_url)
    headers["Accept"] = "text/x-patch"
    owned = client is None
    http = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        response = http.get(f"{base}/api/v1/automation/plans/{plan_id}/patch", headers=headers)
    except httpx.HTTPError as exc:
        raise PlanCliError(f"Could not reach GreenKube: {type(exc).__name__}.") from None
    finally:
        if owned:
            http.close()
    if response.status_code != 200:
        raise PlanCliError(_error_text(response))
    meta = {
        "branch": response.headers.get("X-GreenKube-Branch", ""),
        "base_branch": response.headers.get("X-GreenKube-Base-Branch", ""),
        "base_revision": response.headers.get("X-GreenKube-Base-Revision", ""),
    }
    if not all(meta.values()):
        raise PlanCliError("GreenKube did not return the plan branch and base revision with the patch.")
    return response.text, meta


def report_delivery(
    plan_id: str, *, pr_url: str, head_sha: Optional[str] = None, api_url: Optional[str] = None, client=None
) -> dict:
    """``POST /automation/plans/{id}/delivery``: link the opened pull request to the plan."""
    import httpx

    base, headers = _api(api_url)
    body: dict[str, str] = {"pr_url": pr_url}
    if head_sha:
        body["head_sha"] = head_sha
    owned = client is None
    http = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        response = http.post(f"{base}/api/v1/automation/plans/{plan_id}/delivery", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise PlanCliError(f"Could not reach GreenKube: {type(exc).__name__}.") from None
    finally:
        if owned:
            http.close()
    if response.status_code not in (200, 201):
        raise PlanCliError(_error_text(response))
    return response.json()


def apply_patch_to_repository(
    patch: str,
    *,
    branch: str,
    base_branch: str,
    base_revision: str,
    repository: Path,
    remote: str = "origin",
    fetch: bool = True,
    push: bool = False,
) -> None:
    """Create ``branch`` on ``base_revision`` in ``repository`` and ``git am --3way`` the patch."""

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args], cwd=repository, capture_output=True, text=True, check=False, timeout=300
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise PlanCliError(f"git {args[0]} failed: {detail[-1] if detail else 'exit ' + str(result.returncode)}")
        return result

    if git("rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise PlanCliError(f"{repository} is not a Git working tree; run the command in the GitOps repository.")
    if git("status", "--porcelain").stdout.strip():
        raise PlanCliError("The working tree has uncommitted changes; commit or stash them first.")
    if git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise PlanCliError(f"Branch {branch} already exists; delete it or switch to it to inspect it.")
    if fetch:
        git("fetch", "--quiet", remote, base_branch)
    if git("cat-file", "-e", f"{base_revision}^{{commit}}", check=False).returncode != 0:
        raise PlanCliError(
            f"The plan's base revision {base_revision[:12]} is not in this repository; fetch {base_branch} first."
        )
    git("switch", "--quiet", "-c", branch, base_revision)
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as handle:
        handle.write(patch)
        patch_path = handle.name
    try:
        result = git("am", "--3way", "--keep-cr", patch_path, check=False)
        if result.returncode != 0:
            git("am", "--abort", check=False)
            raise PlanCliError("The patch did not apply; the plan is probably stale. Preview a new plan.")
    finally:
        os.unlink(patch_path)
    if push:
        git("push", "--quiet", "--set-upstream", remote, branch)


@app.command("apply-patch")
def apply_patch(
    plan_id: str = typer.Argument(..., help="Approved plan id (pln-...)."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="GreenKube URL (default: GREENKUBE_API_URL)."),
    repository: Path = typer.Option(Path("."), "--repo", help="Local clone of the GitOps repository."),
    remote: str = typer.Option("origin", "--remote", help="Remote that holds the base branch."),
    fetch: bool = typer.Option(True, "--fetch/--no-fetch", help="Fetch the base branch first."),
    push: bool = typer.Option(False, "--push", help="Push the plan branch to the remote."),
) -> None:
    """Apply an approved plan as one commit on its deterministic branch."""
    try:
        patch, meta = download_patch(plan_id, api_url=api_url)
        apply_patch_to_repository(
            patch,
            branch=meta["branch"],
            base_branch=meta["base_branch"],
            base_revision=meta["base_revision"],
            repository=repository,
            remote=remote,
            fetch=fetch,
            push=push,
        )
    except PlanCliError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Plan {plan_id} applied on branch {meta['branch']} (base {meta['base_branch']}).")
    if not push:
        typer.echo(f"Push it: git push --set-upstream {remote} {meta['branch']}")
    typer.echo(
        f"Open a pull request from {meta['branch']} into {meta['base_branch']}: GreenKube links it by its branch "
        f"name. To link it now: greenkube plan report-delivery {plan_id} --pr-url <url>"
    )


@app.command("report-delivery")
def report_delivery_command(
    plan_id: str = typer.Argument(..., help="Approved plan id (pln-...)."),
    pr_url: str = typer.Option(..., "--pr-url", help="Web URL of the pull request you opened."),
    head_sha: Optional[str] = typer.Option(None, "--head-sha", help="Head commit of the pull request (CI)."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="GreenKube URL (default: GREENKUBE_API_URL)."),
) -> None:
    """Report the pull request you opened for a plan ("I opened PR <url>")."""
    try:
        result = report_delivery(plan_id, pr_url=pr_url, head_sha=head_sha, api_url=api_url)
    except PlanCliError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    pull_request = result.get("pull_request", {})
    state = "linked" if result.get("created") else "already linked"
    typer.echo(f"Pull request #{pull_request.get('number')} {state} to plan {plan_id}; GreenKube tracks it now.")


def _error_text(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = payload.get("code") if isinstance(payload, dict) else None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        code, detail = detail.get("code", code), detail.get("message") or detail.get("detail")
    text = f"GreenKube answered {response.status_code}"
    if code:
        text += f" ({code})"
    return text + (f": {detail}" if isinstance(detail, str) and detail else ".")
