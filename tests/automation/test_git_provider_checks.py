"""Successful authenticated calls mark the Git provider verified (archive fetch, PR polling, ...)."""

from typing import cast

import pytest
import respx
from httpx import Response

from greenkube.automation.git.base import GitProviderError, GitProviderTransientError
from greenkube.automation.git.registry import GitProviderRegistry, ProviderCheckRecorder
from greenkube.storage.base_gitops_repository import GitOpsProviderRepository
from tests.automation.test_git_provider_registry import (  # pyrefly: ignore[missing-import]
    StubProviderRepository,
    StubSecretResolver,
    _config,
    _repository,
)


class RecordingRepository(StubProviderRepository):
    def __init__(self):
        super().__init__(_config(), ["acme/*"])
        self.checks: list[tuple[str, str, str | None]] = []

    async def record_provider_check(self, provider_id: str, status: str, *, error_code: str | None = None) -> bool:
        self.checks.append((provider_id, status, error_code))
        return True


def _registry() -> tuple[GitProviderRegistry, RecordingRepository]:
    repository = RecordingRepository()
    return GitProviderRegistry(cast(GitOpsProviderRepository, repository), StubSecretResolver()), repository


REVISION = "b" * 40

_PULL = {
    "number": 7,
    "html_url": "https://github.test/acme/manifests/pull/7",
    "state": "open",
    "merged": False,
    "head": {"ref": "greenkube/plan", "sha": "a" * 40},
    "base": {"ref": "main"},
}


@pytest.mark.asyncio
async def test_archive_fetch_and_pull_request_polling_verify_the_provider():
    registry, repository = _registry()
    provider = await registry.get_provider("github-main", _repository())
    with respx.mock:
        respx.get(f"https://api.github.test/repos/acme/manifests/tarball/{REVISION}").mock(
            return_value=Response(200, content=b"archive")
        )
        respx.get("https://api.github.test/repos/acme/manifests/pulls/7").mock(return_value=Response(200, json=_PULL))
        assert await provider.get_archive(_repository(), REVISION, max_bytes=1024) == b"archive"
        assert repository.checks == [("github-main", "verified", None)]
        await provider.get_pull_request(_repository(), 7)
    await provider.close()

    # The unchanged state is not rewritten within the refresh interval.
    assert repository.checks == [("github-main", "verified", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code"), [(401, "auth_failed"), (403, "permission_denied")])
async def test_a_refused_token_marks_the_provider_in_error(status, code):
    registry, repository = _registry()
    provider = await registry.get_provider("github-main", _repository())
    with respx.mock:
        respx.get("https://api.github.test/repos/acme/manifests/pulls/7").mock(
            return_value=Response(status, json={"message": "Bad credentials"})
        )
        with pytest.raises(GitProviderError):
            await provider.get_pull_request(_repository(), 7)
    await provider.close()
    assert repository.checks == [("github-main", "error", code)]


@pytest.mark.asyncio
async def test_state_changes_are_written_at_once_and_unchanged_states_after_the_refresh():
    now = [0.0]
    repository = RecordingRepository()
    recorder = ProviderCheckRecorder(repository, refresh_seconds=300, clock=lambda: now[0])
    checks = recorder.bind("github-main")

    await checks.succeeded()
    await checks.succeeded()
    await checks.failed(GitProviderTransientError("rate limited", status_code=403))
    now[0] = 10
    await checks.failed(GitProviderError("refused", status_code=401))
    await checks.failed(GitProviderError("not found", status_code=404))
    await checks.succeeded()
    now[0] = 400
    await checks.succeeded()

    assert repository.checks == [
        ("github-main", "verified", None),
        ("github-main", "error", "auth_failed"),
        ("github-main", "verified", None),
        ("github-main", "verified", None),
    ]


@pytest.mark.asyncio
async def test_a_failed_state_write_never_fails_the_call():
    class Broken(RecordingRepository):
        async def record_provider_check(self, *args, **kwargs) -> bool:
            raise RuntimeError("database down")

    await ProviderCheckRecorder(Broken()).bind("github-main").succeeded()
