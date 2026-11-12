from __future__ import annotations

import gzip
import io
import tarfile

import pytest

from greenkube.gitops.renderers.charts import (
    ChartArchiveError,
    ChartDigestMismatchError,
    ChartDigestUnavailableError,
    ChartFetchLimitError,
    ChartFetchLimits,
    ChartHostNotAllowedError,
    ChartNotFoundError,
    ChartReference,
    ChartReferenceError,
    ChartRegistryAuthRequiredError,
    ChartVersionUnresolvedError,
    RemoteChartFetcher,
    RemoteChartsDisabledError,
)
from tests.gitops.chart_registry import (  # pyrefly: ignore[missing-import]
    HTTP_REPOSITORY,
    OCI_BLOB_HOST,
    OCI_REGISTRY,
    FakeChartRegistry,
    chart_files,
    package_chart,
    public_resolver,
    sha256,
)


def _fetcher(registry: FakeChartRegistry, **kwargs) -> RemoteChartFetcher:
    return RemoteChartFetcher(transport=registry.transport(), resolver=public_resolver, **kwargs)


def _oci() -> ChartReference:
    # Argo CD writes OCI Helm repositories without a scheme.
    return ChartReference.parse("registry-1.docker.io/bitnamicharts", "nginx", "18.3.1")


def test_oci_chart_is_fetched_anonymously_and_verified_against_manifest_and_layer_digests():
    registry = FakeChartRegistry()

    chart = _fetcher(registry).fetch(_oci())

    assert chart.reference.repository == "oci://registry-1.docker.io/bitnamicharts"
    assert chart.name == "nginx"
    assert chart.version == "18.3.1"
    assert chart.digest == registry.manifest_digest
    assert chart.archive_digest == sha256(registry.archive)
    assert set(chart.files) == set(chart_files())
    assert "charts/common/Chart.yaml" in chart.files
    assert chart.provenance == (
        "remote Helm chart oci://registry-1.docker.io/bitnamicharts/nginx:18.3.1 rendered from "
        f"{registry.manifest_digest}"
    )
    assert registry.count("auth.docker.io") == 1
    assert registry.count(OCI_BLOB_HOST) == 1


def test_oci_chart_pinned_to_the_deployed_digest_fails_closed_on_a_moved_tag():
    registry = FakeChartRegistry()
    reference = ChartReference.parse(
        "oci://registry-1.docker.io/bitnamicharts",
        "nginx",
        "18.3.1",
        expected_digest="sha256:" + "0" * 64,
    )

    with pytest.raises(ChartDigestMismatchError, match="not the deployed digest"):
        _fetcher(registry).fetch(reference)


def test_tampered_oci_blob_fails_closed_on_the_layer_digest():
    registry = FakeChartRegistry()
    registry.served_archive = package_chart({**chart_files(), "values.yaml": b"replicaCount: 9\n"})

    with pytest.raises(ChartDigestMismatchError, match="chart layer digest"):
        _fetcher(registry).fetch(_oci())


def test_http_repository_chart_is_verified_against_the_index_digest():
    registry = FakeChartRegistry()

    chart = _fetcher(registry).fetch(ChartReference.parse(HTTP_REPOSITORY, "nginx", "18.3.1"))

    assert chart.digest == sha256(registry.archive)
    assert chart.files["values.yaml"].startswith(b"replicaCount: 1")


def test_http_repository_tampered_archive_and_missing_digest_fail_closed():
    tampered = FakeChartRegistry()
    tampered.served_archive = package_chart({**chart_files(), "values.yaml": b"replicaCount: 9\n"})
    with pytest.raises(ChartDigestMismatchError):
        _fetcher(tampered).fetch(ChartReference.parse(HTTP_REPOSITORY, "nginx", "18.3.1"))

    unsigned = FakeChartRegistry(index_digest="not-a-digest")
    with pytest.raises(ChartDigestUnavailableError):
        _fetcher(unsigned).fetch(ChartReference.parse(HTTP_REPOSITORY, "nginx", "18.3.1"))


def test_unknown_chart_version_is_not_found():
    registry = FakeChartRegistry()

    with pytest.raises(ChartNotFoundError):
        _fetcher(registry).fetch(ChartReference.parse(HTTP_REPOSITORY, "nginx", "19.0.0"))


def test_version_ranges_and_tags_are_never_rendered():
    for version in ("18.x", ">=18.0.0", "latest", "", None):
        with pytest.raises(ChartVersionUnresolvedError):
            ChartReference.parse(HTTP_REPOSITORY, "nginx", version)
    with pytest.raises(ChartReferenceError):
        ChartReference.parse("ftp://charts.example.test", "nginx", "1.0.0")
    with pytest.raises(ChartReferenceError):
        ChartReference.parse("https://user:secret@charts.example.test", "nginx", "1.0.0")


def test_explicit_allowlist_restricts_every_host_including_token_and_blob_hosts():
    registry = FakeChartRegistry()

    with pytest.raises(ChartHostNotAllowedError, match="auth.docker.io"):
        _fetcher(registry, allowed_hosts=[OCI_REGISTRY]).fetch(_oci())

    chart = _fetcher(registry, allowed_hosts=[OCI_REGISTRY, "auth.docker.io", "*.docker.com"]).fetch(_oci())
    assert chart.name == "nginx"


def test_internal_and_metadata_hosts_are_refused_even_without_an_allowlist():
    registry = FakeChartRegistry()
    fetcher = RemoteChartFetcher(transport=registry.transport(), resolver=lambda _host: ["169.254.169.254"])

    with pytest.raises(ChartHostNotAllowedError, match="link-local"):
        fetcher.fetch(_oci())
    with pytest.raises(ChartHostNotAllowedError, match="metadata"):
        _fetcher(registry).check_url("https://metadata.google.internal/computeMetadata/v1")
    with pytest.raises(ChartHostNotAllowedError):
        _fetcher(registry).check_url("https://127.0.0.1/index.yaml")


def test_plain_http_repositories_must_be_explicitly_allowed():
    registry = FakeChartRegistry()

    with pytest.raises(ChartHostNotAllowedError, match="plain-HTTP"):
        _fetcher(registry).check_url("http://chartmuseum.tools.svc:8080/index.yaml")
    assert _fetcher(registry, allowed_hosts=["chartmuseum.tools.svc:8080"]).check_url(
        "http://chartmuseum.tools.svc:8080/index.yaml"
    )


def test_registries_requiring_credentials_fail_closed_with_a_reason():
    registry = FakeChartRegistry(token_status=401)

    with pytest.raises(ChartRegistryAuthRequiredError) as error:
        _fetcher(registry).fetch(_oci())
    assert error.value.reason == "chart-registry-auth-required"


def test_disabled_remote_charts_never_contact_a_registry():
    registry = FakeChartRegistry()

    with pytest.raises(RemoteChartsDisabledError):
        _fetcher(registry, enabled=False).fetch(_oci())
    assert registry.requests == []


def test_verified_archives_and_resolutions_are_cached():
    registry = FakeChartRegistry()
    fetcher = _fetcher(registry)

    first = fetcher.fetch(_oci())
    requests_after_first = len(registry.requests)
    second = fetcher.fetch(_oci())

    assert second is first
    assert len(registry.requests) == requests_after_first
    assert registry.count(OCI_BLOB_HOST) == 1


def test_download_and_unpack_limits_fail_closed():
    registry = FakeChartRegistry()

    with pytest.raises(ChartFetchLimitError):
        _fetcher(registry, limits=ChartFetchLimits(max_archive_bytes=64)).fetch(_oci())
    with pytest.raises(ChartFetchLimitError):
        _fetcher(registry, limits=ChartFetchLimits(max_files=3)).fetch(_oci())


def _archive_with(member: tarfile.TarInfo, content: bytes = b"") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        chart = b"apiVersion: v2\nname: nginx\nversion: 18.3.1\n"
        info = tarfile.TarInfo("nginx/Chart.yaml")
        info.size = len(chart)
        archive.addfile(info, io.BytesIO(chart))
        archive.addfile(member, io.BytesIO(content) if member.isreg() else None)
    return gzip.compress(raw.getvalue(), mtime=0)


@pytest.mark.parametrize(
    "member",
    [
        tarfile.TarInfo("nginx/../escape.yaml"),
        tarfile.TarInfo("/etc/passwd"),
        tarfile.TarInfo("other/Chart.yaml"),
    ],
)
def test_unsafe_archive_paths_are_rejected(member: tarfile.TarInfo):
    member.size = 0
    registry = FakeChartRegistry(archive=_archive_with(member))

    with pytest.raises(ChartArchiveError):
        _fetcher(registry).fetch(_oci())


def test_links_in_chart_archives_are_rejected():
    link = tarfile.TarInfo("nginx/templates/link.yaml")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    registry = FakeChartRegistry(archive=_archive_with(link))

    with pytest.raises(ChartArchiveError, match="regular files"):
        _fetcher(registry).fetch(_oci())


def test_packaged_dependencies_are_unpacked_into_the_charts_directory():
    files = chart_files()
    common = {key.removeprefix("charts/common/"): value for key, value in files.items() if key.startswith("charts/")}
    parent = {key: value for key, value in files.items() if not key.startswith("charts/")}
    parent["charts/common-2.27.0.tgz"] = package_chart(common, top_level="common")
    registry = FakeChartRegistry(archive=package_chart(parent))

    chart = _fetcher(registry).fetch(_oci())

    assert "charts/common-2.27.0.tgz" not in chart.files
    assert chart.files["charts/common/Chart.yaml"] == files["charts/common/Chart.yaml"]


def test_archive_metadata_must_match_the_requested_chart():
    registry = FakeChartRegistry(
        archive=package_chart({**chart_files(), "Chart.yaml": b"apiVersion: v2\nname: nginx\nversion: 1.0.0\n"})
    )

    with pytest.raises(ChartArchiveError, match="chart version"):
        _fetcher(registry).fetch(_oci())


def test_fetcher_settings_come_from_the_configuration(monkeypatch: pytest.MonkeyPatch):
    from greenkube.core.config import Config

    settings = Config(
        GITOPS_HELM_REMOTE_CHARTS=False, GITOPS_HELM_CHART_HOSTS='["Registry-1.Docker.io", "*.docker.com"]'
    )
    monkeypatch.setattr("greenkube.core.config.get_config", lambda: settings)

    fetcher = RemoteChartFetcher.from_settings()

    assert fetcher.enabled is False
    assert fetcher.allowed_hosts == ("registry-1.docker.io", "*.docker.com")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("GITOPS_HELM_CHART_HOSTS", '["https://ghcr.io/charts"]'),
        ("GITOPS_HELM_CHART_HOSTS", '"ghcr.io"'),
        ("GITOPS_HELM_CHART_MAPPINGS", '[{"chart": "nginx"}]'),
        ("GITOPS_HELM_CHART_MAPPINGS", '[{"chart": "nginx", "path": "resources.limits"}]'),
    ],
)
def test_invalid_remote_chart_settings_are_rejected(field: str, value: str):
    from pydantic import ValidationError

    from greenkube.core.config import Config

    with pytest.raises(ValidationError, match=field):
        Config(**{field: value})


def test_a_cached_archive_reached_through_another_repository_reports_that_repository():
    registry = FakeChartRegistry()
    fetcher = _fetcher(registry)

    fetcher.fetch(_oci())
    via_http = fetcher.fetch(ChartReference.parse(HTTP_REPOSITORY, "nginx", "18.3.1"))

    assert via_http.reference.repository == HTTP_REPOSITORY
    assert via_http.digest == sha256(registry.archive)
    assert registry.count("charts.bitnami.com", "/bitnami/nginx-") == 0
