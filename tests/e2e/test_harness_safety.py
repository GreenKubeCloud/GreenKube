"""Safety of the destructive e2e scripts (hack/e2e), run against fake CLIs.

``kubectl``, ``helm`` and ``curl`` are replaced by stubs that log their calls
and serve canned objects, so these tests run in the normal suite without a
cluster. They check that the harness never touches a cluster other than
``E2E_KUBE_CONTEXT`` and never deletes or upgrades objects it did not create.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = REPO_ROOT / "hack" / "e2e"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq")

FAKE_KUBECTL = r"""#!/usr/bin/env bash
printf 'kubectl %s\n' "$*" >>"${FAKE_LOG}"
args=("$@")
if [[ "$1" == "config" && "$2" == "current-context" ]]; then
  printf '%s\n' "${FAKE_CONTEXT}"
  exit 0
fi
# Drop "-n <namespace>" to find the verb.
if [[ "$1" == "-n" ]]; then args=("${@:3}"); fi
if [[ "${args[0]}" == "get" && "${args[1]}" != "crd" ]]; then
  file="${FAKE_OBJECTS}/${args[1]}_${args[2]}.json"
  [[ -f "${file}" ]] || { echo "not found" >&2; exit 1; }
  cat "${file}"
  exit 0
fi
[[ "${args[0]}" == "get" ]] && exit 1
cat >/dev/null 2>&1 || true
exit 0
"""

FAKE_MINIKUBE = r"""#!/usr/bin/env bash
printf 'minikube %s\n' "$*" >>"${FAKE_LOG}"
# Like the real `minikube ssh`, read stdin.
cat >/dev/null 2>&1 || true
exit 0
"""

FAKE_HELM = r"""#!/usr/bin/env bash
printf 'helm %s\n' "$*" >>"${FAKE_LOG}"
exit 0
"""

FAKE_CURL = r"""#!/usr/bin/env bash
printf 'curl %s\n' "$*" >>"${FAKE_LOG}"
cat >/dev/null 2>&1 || true
# PATCH /user/settings probes the write:user scope (gitea_call prints the status).
if [[ "$*" == *"/user/settings"* ]]; then
  printf '%s' "${FAKE_USER_SETTINGS_STATUS:-200}"
  exit 0
fi
printf '%s' "${FAKE_GITEA_REPO}"
"""


def namespace(name: str, labels: dict[str, str]) -> dict:
    return {"kind": "Namespace", "metadata": {"name": name, "labels": labels}}


@pytest.fixture
def harness(tmp_path: Path):
    """Return a runner for a hack/e2e script against the fake CLIs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("kubectl", FAKE_KUBECTL),
        ("helm", FAKE_HELM),
        ("curl", FAKE_CURL),
        ("minikube", FAKE_MINIKUBE),
    ):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    objects = tmp_path / "objects"
    objects.mkdir()
    log = tmp_path / "calls.log"
    log.touch()

    def run(script: str, *, context: str = "minikube", objs: dict[str, dict] | None = None, **extra: str):
        for key, value in (objs or {}).items():
            (objects / f"{key}.json").write_text(json.dumps(value))
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "E2E_STATE_DIR": str(tmp_path / "state"),
            "GITEA_TOKEN": "dummy-token",
            "FAKE_LOG": str(log),
            "FAKE_CONTEXT": context,
            "FAKE_OBJECTS": str(objects),
            "FAKE_GITEA_REPO": json.dumps({"description": "someone else's repository", "login": "greenkube"}),
            **extra,
        }
        result = subprocess.run(
            ["bash", str(E2E_DIR / script)],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result, log.read_text().splitlines()

    return run


def mutations(calls: list[str]) -> list[str]:
    destructive = ("delete", "uninstall", "upgrade", "apply", "DELETE", "label namespace")
    return [call for call in calls if any(word in call for word in destructive)]


@pytest.mark.parametrize("script", ["cleanup.sh", "install.sh", "setup-gitops.sh", "setup-repo.sh", "run.sh"])
def test_scripts_refuse_another_kube_context(harness, script: str) -> None:
    result, calls = harness(script, context="prod-eu-1")

    assert result.returncode != 0
    assert "prod-eu-1" in result.stderr
    assert "E2E_KUBE_CONTEXT" in result.stderr
    assert mutations(calls) == []


def test_cleanup_leaves_unlabelled_objects_alone(harness) -> None:
    objs = {
        "namespace_greenkube": namespace("greenkube", {}),
        "namespace_greenkube-e2e-apps": namespace("greenkube-e2e-apps", {}),
        "application_greenkube-e2e-quickstart": {"metadata": {"name": "greenkube-e2e-quickstart", "labels": {}}},
    }
    result, calls = harness("cleanup.sh", objs=objs)

    assert result.returncode == 0, result.stderr
    assert mutations(calls) == []
    assert "Leaving namespace greenkube" in result.stderr
    assert "Leaving Gitea repository" in result.stderr


def test_cleanup_removes_labelled_objects(harness) -> None:
    owned = {"greenkube.io/e2e": "true"}
    objs = {
        "namespace_greenkube": namespace("greenkube", owned),
        "namespace_greenkube-e2e-apps": namespace("greenkube-e2e-apps", owned),
        "application_greenkube-e2e-quickstart": {"metadata": {"name": "greenkube-e2e-quickstart", "labels": owned}},
    }
    marker = json.dumps({"description": "GreenKube quickstart sample app (managed by hack/e2e)"})
    result, calls = harness("cleanup.sh", objs=objs, FAKE_GITEA_REPO=marker)

    assert result.returncode == 0, result.stderr
    joined = "\n".join(calls)
    assert "delete application greenkube-e2e-quickstart" in joined
    assert "kubectl delete namespace greenkube-e2e-apps" in joined
    assert "helm -n greenkube uninstall greenkube" in joined
    assert "kubectl delete namespace greenkube " in joined
    assert "-X DELETE" in joined


def test_cleanup_removes_every_leftover_hostpath_volume(harness) -> None:
    # The provisioner reuses a claim's directory: a database volume left behind
    # hands the old data (and its old password) to the next install. `minikube
    # ssh` reads stdin, so it must not swallow the rest of the volume list.
    def volume(name: str, claim: str) -> dict:
        return {
            "metadata": {"name": name},
            "spec": {
                "claimRef": {"namespace": "greenkube", "name": claim},
                "hostPath": {"path": f"/tmp/hostpath-provisioner/greenkube/{claim}"},
            },
        }

    owned = {"greenkube.io/e2e": "true"}
    objs = {
        "namespace_greenkube": namespace("greenkube", owned),
        "pv_-o": {
            "items": [volume("pv-backup", "greenkube-postgres-backup"), volume("pv-data", "data-greenkube-postgres-0")]
        },
    }
    result, calls = harness("cleanup.sh", objs=objs, E2E_TIMEOUT_VOLUMES="0")

    assert result.returncode == 0, result.stderr
    removed = [call for call in calls if call.startswith("minikube ssh")]
    assert any("greenkube-postgres-backup" in call for call in removed), calls
    assert any("data-greenkube-postgres-0" in call for call in removed), calls
    assert any("delete pv pv-data" in call for call in calls), calls


def test_cleanup_adopts_unlabelled_objects_on_request(harness) -> None:
    objs = {"namespace_greenkube": namespace("greenkube", {})}
    result, calls = harness("cleanup.sh", objs=objs, E2E_ADOPT="1")

    assert result.returncode == 0, result.stderr
    assert any("uninstall greenkube" in call for call in calls)


def test_install_refuses_a_namespace_it_did_not_create(harness) -> None:
    objs = {"namespace_greenkube": namespace("greenkube", {"team": "platform"})}
    result, calls = harness("install.sh", objs=objs)

    assert result.returncode != 0
    assert "was not created by the harness" in result.stderr
    assert mutations(calls) == []


def test_install_waits_for_the_one_install_deployments(harness) -> None:
    objs = {"namespace_greenkube": namespace("greenkube", {"greenkube.io/e2e": "true"})}
    _, calls = harness("install.sh", objs=objs)

    rollouts = [call for call in calls if "rollout status" in call]
    assert any("deployment/greenkube-controller" in call for call in rollouts), calls
    assert any("deployment/greenkube-api" in call for call in rollouts), calls
    # The 0.3.x standalone Deployment no longer exists.
    assert not any("deployment/greenkube " in call for call in rollouts)


def test_setup_gitops_refuses_an_existing_application_it_did_not_create(harness) -> None:
    objs = {
        "namespace_greenkube-e2e-apps": namespace("greenkube-e2e-apps", {"greenkube.io/e2e": "true"}),
        "application_greenkube-e2e-quickstart": {"metadata": {"name": "greenkube-e2e-quickstart"}},
    }
    result, calls = harness("setup-gitops.sh", objs=objs)

    assert result.returncode != 0
    assert "application argocd/greenkube-e2e-quickstart exists but was not created by the harness" in result.stderr
    assert "Applying Argo CD Application" not in result.stderr
    assert not any("annotate application" in call for call in calls)


def test_setup_repo_refuses_a_repository_it_did_not_create(harness) -> None:
    result, calls = harness("setup-repo.sh", GITEA_RESET_REPO="1")

    assert result.returncode != 0
    assert "was not created by the harness" in result.stderr
    assert not any("-X DELETE" in call or "-X POST" in call for call in calls)


OWNED_REPO = json.dumps({"description": "GreenKube quickstart sample app (managed by hack/e2e)", "login": "greenkube"})


def test_setup_repo_keeps_the_repository_when_the_token_cannot_recreate_it(harness) -> None:
    # A token with write:repository but not write:user may delete the repository,
    # but Gitea then refuses POST /user/repos: the reset must stop before the DELETE.
    result, calls = harness(
        "setup-repo.sh", GITEA_RESET_REPO="1", FAKE_GITEA_REPO=OWNED_REPO, FAKE_USER_SETTINGS_STATUS="403"
    )

    assert result.returncode != 0
    assert "write:user" in result.stderr
    assert "Nothing was deleted" in result.stderr
    assert not any("-X DELETE" in call or "-X POST" in call for call in calls)


def test_setup_repo_resets_the_repository_when_the_token_can_recreate_it(harness) -> None:
    _, calls = harness("setup-repo.sh", GITEA_RESET_REPO="1", FAKE_GITEA_REPO=OWNED_REPO)

    joined = "\n".join(calls)
    assert "-X PATCH" in joined and "/user/settings" in joined
    assert "-X DELETE" in joined


def test_apps_namespace_cannot_be_overridden(harness) -> None:
    result, calls = harness("cleanup.sh", APPS_NAMESPACE="production")

    assert result.returncode != 0
    assert "APPS_NAMESPACE cannot be changed" in result.stderr
    assert mutations(calls) == []


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl (used with --local only)")
@pytest.mark.parametrize("manifest", ["argocd-application.yaml", "flux-kustomization.yaml"])
def test_label_owned_keeps_every_object_of_a_manifest(tmp_path: Path, manifest: str) -> None:
    source = REPO_ROOT / "docs" / "examples" / "quickstart" / manifest
    result = subprocess.run(
        ["bash", "-c", f'source "{E2E_DIR / "lib.sh"}" && label_owned "{source}"'],
        env={**os.environ, "E2E_STATE_DIR": str(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    items = json.loads(result.stdout)["items"]
    expected_kinds = {
        "argocd-application.yaml": ["Application"],
        "flux-kustomization.yaml": ["GitRepository", "Kustomization"],
    }
    assert [item["kind"] for item in items] == expected_kinds[manifest]
    assert all(item["metadata"]["labels"]["greenkube.io/e2e"] == "true" for item in items)


def test_gitea_token_has_no_private_default() -> None:
    lib = (E2E_DIR / "lib.sh").read_text()

    assert "Documents/Entreprise" not in lib
    assert ': "${GITEA_TOKEN_FILE:=}"' in lib
