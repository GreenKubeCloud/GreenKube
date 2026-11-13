"""Digest-verified pre-fetch of remote Helm charts (HTTP repositories and OCI registries).

The renderer sandbox has no network: remote charts are fetched by the controller,
verified against the digest published by their repository (HTTP ``index.yaml``
digest, or the OCI manifest and layer digests), unpacked in memory with hard
limits and staged into the render workspace as a local chart directory. Only
hosts named by the discovered chart source (narrowed by an optional allowlist)
are contacted, never loopback, link-local or metadata addresses.
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import io
import ipaddress
import json
import re
import socket
import tarfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from greenkube.gitops.renderers.helm import HelmError
from greenkube.gitops.sha import SHA256_DIGEST

HELM_CONFIG_MEDIA_TYPE = "application/vnd.cncf.helm.config.v1+json"
HELM_CHART_MEDIA_TYPES = frozenset(
    {
        "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
        "application/tar+gzip",
    }
)
_MANIFEST_MEDIA_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
_INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHART_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
_OCI_PATH_COMPONENT = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_EXACT_VERSION = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_HOST_PATTERN = re.compile(r"^(?:\*\.)?[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::\d{1,5})?$")
_BEARER_PARAMETER = re.compile(r'(\w+)="([^"]*)"')
_METADATA_HOSTNAMES = frozenset({"localhost", "metadata", "metadata.google.internal", "instance-data"})
# Cloud metadata endpoints outside the link-local ranges (AWS IPv6 IMDS, Alibaba Cloud).
_METADATA_ADDRESSES = frozenset(ipaddress.ip_address(value) for value in ("fd00:ec2::254", "100.100.100.200"))
_MAX_REDIRECTS = 5
_MAX_NESTED_CHART_DEPTH = 4


class ChartFetchError(HelmError):
    """A remote chart could not be fetched, verified or unpacked; ``reason`` is a stable code."""

    reason = "remote-chart-unavailable"

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        if reason is not None:
            self.reason = reason
        super().__init__(f"{self.reason}: {message}")


class RemoteChartsDisabledError(ChartFetchError):
    reason = "remote-charts-disabled"


class ChartReferenceError(ChartFetchError):
    reason = "chart-reference-invalid"


class ChartVersionUnresolvedError(ChartFetchError):
    reason = "chart-version-unresolved"


class ChartHostNotAllowedError(ChartFetchError):
    reason = "chart-host-not-allowed"


class ChartNotFoundError(ChartFetchError):
    reason = "chart-not-found"


class ChartRegistryAuthRequiredError(ChartFetchError):
    reason = "chart-registry-auth-required"


class ChartDigestUnavailableError(ChartFetchError):
    reason = "chart-digest-unavailable"


class ChartDigestMismatchError(ChartFetchError):
    reason = "chart-digest-mismatch"


class ChartArchiveError(ChartFetchError):
    reason = "chart-archive-invalid"


class ChartFetchLimitError(ChartFetchError):
    reason = "chart-size-limit"


@dataclass(frozen=True, slots=True)
class ChartFetchLimits:
    """Hard bounds on everything a chart fetch downloads or unpacks."""

    timeout_seconds: float = 30.0
    max_index_bytes: int = 64 * 1024 * 1024
    max_manifest_bytes: int = 4 * 1024 * 1024
    max_archive_bytes: int = 16 * 1024 * 1024
    max_files: int = 4_096
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024
    cache_entries: int = 16
    resolution_ttl_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class ChartReference:
    """An exact chart version in an HTTP Helm repository or an OCI registry."""

    repository: str
    name: str
    version: str
    expected_digest: str | None = None

    @classmethod
    def parse(
        cls,
        repository: object,
        name: object,
        version: object,
        *,
        expected_digest: object = None,
    ) -> ChartReference:
        if not isinstance(repository, str) or not repository.strip() or repository != repository.strip():
            raise ChartReferenceError("the chart repository URL must be a non-empty trimmed string")
        if not isinstance(name, str) or not name or name != name.strip():
            raise ChartReferenceError("the chart name must be a non-empty trimmed string")
        if not isinstance(version, str) or not version or version != version.strip():
            raise ChartVersionUnresolvedError("the chart version is unknown")
        if _EXACT_VERSION.fullmatch(version) is None:
            raise ChartVersionUnresolvedError(
                f"chart version {version!r} is not an exact version; ranges and tags are not rendered"
            )
        normalized = _normalize_repository(repository)
        if normalized.startswith("oci://"):
            parts = name.split("/")
            if any(_OCI_PATH_COMPONENT.fullmatch(part) is None for part in parts):
                raise ChartReferenceError(f"OCI chart name {name!r} is invalid")
        elif _CHART_NAME.fullmatch(name) is None:
            raise ChartReferenceError(f"chart name {name!r} is invalid")
        digest: str | None = None
        if expected_digest not in (None, ""):
            if not isinstance(expected_digest, str):
                raise ChartReferenceError("the pinned chart digest must be a string")
            digest = expected_digest.strip().lower()
            if _HEX_SHA256.fullmatch(digest):
                digest = f"sha256:{digest}"
            if SHA256_DIGEST.fullmatch(digest) is None:
                raise ChartReferenceError("the pinned chart digest must be a sha256 digest")
        return cls(repository=normalized, name=name, version=version, expected_digest=digest)

    @property
    def is_oci(self) -> bool:
        return self.repository.startswith("oci://")

    @property
    def chart_name(self) -> str:
        """The chart's own name (the last path component of an OCI reference)."""
        return self.name.rsplit("/", 1)[-1]

    @property
    def display(self) -> str:
        if self.is_oci:
            return f"{self.repository}/{self.name}:{self.version}"
        return f"{self.repository} {self.name}@{self.version}"


@dataclass(frozen=True, slots=True)
class FetchedChart:
    """A verified, unpacked chart: ``files`` are paths relative to the chart directory."""

    reference: ChartReference
    name: str
    version: str
    digest: str
    archive_digest: str
    files: Mapping[str, bytes] = field(repr=False)

    @property
    def provenance(self) -> str:
        """One-line record of what was rendered, for plan evidence."""
        return f"remote Helm chart {self.reference.display} rendered from {self.digest}"

    def file_text(self, path: str) -> str | None:
        content = self.files.get(path)
        if content is None:
            return None
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return None


Resolver = Callable[[str], Iterable[str]]


class RemoteChartFetcher:
    """Fetch, verify and unpack remote charts with bounded, policy-checked HTTP."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        allowed_hosts: Sequence[str] = (),
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        limits: ChartFetchLimits | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.allowed_hosts = tuple(_normalize_host_patterns(allowed_hosts))
        self.limits = limits or ChartFetchLimits()
        self._transport = transport
        self._resolver = resolver or _default_resolver
        self._clock = clock or time.monotonic
        self._archives: OrderedDict[str, FetchedChart] = OrderedDict()
        self._resolutions: dict[ChartReference, tuple[float, str, str]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls) -> RemoteChartFetcher:
        """Build the fetcher from GITOPS_HELM_REMOTE_CHARTS and GITOPS_HELM_CHART_HOSTS."""
        from greenkube.core.config import get_config

        settings = get_config()
        return cls(
            enabled=bool(getattr(settings, "GITOPS_HELM_REMOTE_CHARTS", True)),
            allowed_hosts=tuple(getattr(settings, "helm_chart_hosts", ())),
        )

    def fetch(self, reference: ChartReference) -> FetchedChart:
        if not isinstance(reference, ChartReference):
            raise ChartReferenceError("a parsed ChartReference is required")
        if not self.enabled:
            raise RemoteChartsDisabledError("remote chart rendering is disabled (GITOPS_HELM_REMOTE_CHARTS=false)")
        with httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(self.limits.timeout_seconds, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            session = _Session(self, client)
            if reference.is_oci:
                digest, archive_digest, archive = self._fetch_oci(session, reference)
            else:
                digest, archive_digest, archive = self._fetch_http(session, reference)
        if not isinstance(archive, FetchedChart):
            chart = _unpack_chart(reference, digest, archive_digest, archive, self.limits)
            self._remember(chart)
            return chart
        cached = archive
        if cached.reference != reference or cached.digest != digest:
            # Same verified archive reached through another reference (repository or pin).
            return replace(cached, reference=reference, digest=digest)
        return cached

    # -- HTTP repositories -------------------------------------------------------------------
    def _fetch_http(self, session: _Session, reference: ChartReference) -> tuple[str, str, bytes | FetchedChart]:
        resolved = self._cached_resolution(reference)
        if resolved is None:
            index_url = f"{reference.repository}/index.yaml"
            body = session.get(index_url, max_bytes=self.limits.max_index_bytes).content
            entry = _index_entry(body, reference)
            raw_urls = entry.get("urls")
            if not isinstance(raw_urls, list) or not raw_urls or not isinstance(raw_urls[0], str):
                raise ChartNotFoundError(f"{reference.display} has no download URL in the repository index")
            archive_url = urljoin(f"{reference.repository}/", raw_urls[0])
            published = entry.get("digest")
            if isinstance(published, str) and _HEX_SHA256.fullmatch(published.strip().lower()):
                archive_digest = f"sha256:{published.strip().lower()}"
            elif reference.expected_digest is not None:
                archive_digest = reference.expected_digest
            else:
                raise ChartDigestUnavailableError(
                    f"the repository index publishes no sha256 digest for {reference.display}"
                )
            if reference.expected_digest is not None and reference.expected_digest != archive_digest:
                raise ChartDigestMismatchError(
                    f"{reference.display} index digest {archive_digest} differs from the pinned digest"
                )
            self._store_resolution(reference, archive_digest, archive_url)
        else:
            archive_digest, archive_url = resolved
        cached = self._cached_archive(archive_digest)
        if cached is not None:
            return archive_digest, archive_digest, cached
        archive = session.get(archive_url, max_bytes=self.limits.max_archive_bytes).content
        _verify_digest(archive, archive_digest, f"{reference.display} archive")
        return archive_digest, archive_digest, archive

    # -- OCI registries ----------------------------------------------------------------------
    def _fetch_oci(self, session: _Session, reference: ChartReference) -> tuple[str, str, bytes | FetchedChart]:
        host, base_path = _split_oci(reference.repository)
        repository = "/".join(part for part in (base_path, reference.name) if part)
        registry = f"https://{host}"
        scope = f"repository:{repository}:pull"
        resolved = self._cached_resolution(reference)
        if resolved is None:
            tag = reference.version.replace("+", "_")
            response = session.get(
                f"{registry}/v2/{repository}/manifests/{tag}",
                max_bytes=self.limits.max_manifest_bytes,
                headers={"Accept": ", ".join((*_MANIFEST_MEDIA_TYPES, *_INDEX_MEDIA_TYPES))},
                registry_scope=scope,
            )
            manifest_bytes = response.content
            manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
            advertised = response.headers.get("docker-content-digest")
            if advertised and advertised.strip().lower() != manifest_digest:
                raise ChartDigestMismatchError(
                    f"{reference.display} manifest digest differs from the registry's Docker-Content-Digest"
                )
            if reference.expected_digest is not None and reference.expected_digest != manifest_digest:
                raise ChartDigestMismatchError(
                    f"{reference.display} resolved to {manifest_digest}, not the deployed digest "
                    f"{reference.expected_digest}"
                )
            layer_digest = _chart_layer_digest(manifest_bytes, reference)
            archive_url = f"{registry}/v2/{repository}/blobs/{layer_digest}"
            self._store_resolution(reference, manifest_digest, archive_url)
            digest = manifest_digest
        else:
            digest, archive_url = resolved
            layer_digest = archive_url.rsplit("/", 1)[-1]
        cached = self._cached_archive(layer_digest)
        if cached is not None:
            return digest, layer_digest, cached
        archive = session.get(
            archive_url,
            max_bytes=self.limits.max_archive_bytes,
            registry_scope=scope,
        ).content
        _verify_digest(archive, layer_digest, f"{reference.display} chart layer")
        return digest, layer_digest, archive

    # -- caches ------------------------------------------------------------------------------
    def _cached_resolution(self, reference: ChartReference) -> tuple[str, str] | None:
        with self._lock:
            entry = self._resolutions.get(reference)
            if entry is None:
                return None
            stored_at, digest, url = entry
            if self._clock() - stored_at > self.limits.resolution_ttl_seconds:
                self._resolutions.pop(reference, None)
                return None
            return digest, url

    def _store_resolution(
        self,
        reference: ChartReference,
        digest: str,
        archive_url: str,
    ) -> None:
        with self._lock:
            self._resolutions[reference] = (self._clock(), digest, archive_url)
            while len(self._resolutions) > self.limits.cache_entries * 4:
                self._resolutions.pop(next(iter(self._resolutions)))

    def _cached_archive(self, archive_digest: str) -> FetchedChart | None:
        with self._lock:
            chart = self._archives.get(archive_digest)
            if chart is not None:
                self._archives.move_to_end(archive_digest)
            return chart

    def _remember(self, chart: FetchedChart) -> None:
        with self._lock:
            self._archives[chart.archive_digest] = chart
            self._archives.move_to_end(chart.archive_digest)
            while len(self._archives) > self.limits.cache_entries:
                self._archives.popitem(last=False)

    # -- egress policy -----------------------------------------------------------------------
    def check_url(self, url: str) -> str:
        """Validate one URL against the egress policy and return it normalized."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise ChartHostNotAllowedError(f"chart URL {url!r} is invalid") from exc
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in {"https", "http"} or not host or parts.username or parts.password:
            raise ChartHostNotAllowedError("chart URLs must be http(s) URLs without credentials")
        host_port = host if port is None else f"{host}:{port}"
        explicitly_allowed = bool(self.allowed_hosts) and any(
            _host_matches(pattern, host, host_port) for pattern in self.allowed_hosts
        )
        if self.allowed_hosts and not explicitly_allowed:
            raise ChartHostNotAllowedError(
                f"chart host {host_port!r} is not in automation.helm.remoteCharts.allowedHosts"
            )
        if parts.scheme == "http" and not explicitly_allowed:
            raise ChartHostNotAllowedError(
                f"plain-HTTP chart host {host_port!r} must be listed in automation.helm.remoteCharts.allowedHosts"
            )
        _reject_internal_host(host, self._resolver)
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query, ""))


class _Session:
    """One fetch: policy-checked, manually redirected, size-bounded GETs with registry tokens."""

    def __init__(self, fetcher: RemoteChartFetcher, client: httpx.Client) -> None:
        self._fetcher = fetcher
        self._client = client
        self._tokens: dict[tuple[str, str], str] = {}

    def get(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
        registry_scope: str | None = None,
    ) -> _Response:
        current = self._fetcher.check_url(url)
        origin = urlsplit(current).netloc
        request_headers = dict(headers or {})
        authenticated = False
        for _ in range(_MAX_REDIRECTS + 1):
            send_headers = dict(request_headers)
            token = self._tokens.get((origin, registry_scope or ""))
            if token is not None and urlsplit(current).netloc == origin:
                send_headers["Authorization"] = f"Bearer {token}"
            response = self._send(current, send_headers, max_bytes)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ChartFetchError(f"chart server redirect from {current} has no location")
                current = self._fetcher.check_url(urljoin(current, location))
                continue
            if response.status_code == 401 and registry_scope is not None and not authenticated:
                challenge = response.headers.get("www-authenticate", "")
                self._tokens[(origin, registry_scope)] = self._registry_token(challenge, registry_scope)
                authenticated = True
                continue
            if response.status_code in {401, 403}:
                raise ChartRegistryAuthRequiredError(
                    f"{origin} requires credentials for this chart; only anonymous pulls are supported"
                )
            if response.status_code == 404:
                raise ChartNotFoundError(f"{current} was not found")
            if response.status_code != 200:
                raise ChartFetchError(f"{current} answered HTTP {response.status_code}")
            return response
        raise ChartFetchError(f"chart download exceeded {_MAX_REDIRECTS} redirects")

    def _registry_token(self, challenge: str, scope: str) -> str:
        scheme, _, parameters = challenge.partition(" ")
        if scheme.lower() != "bearer":
            raise ChartRegistryAuthRequiredError("the registry requires basic credentials; only anonymous pulls work")
        values = dict(_BEARER_PARAMETER.findall(parameters))
        realm = values.get("realm")
        if not realm:
            raise ChartRegistryAuthRequiredError("the registry token challenge names no realm")
        query = {"scope": values.get("scope") or scope}
        if values.get("service"):
            query["service"] = values["service"]
        separator = "&" if "?" in realm else "?"
        response = self.get(f"{realm}{separator}{urlencode(query)}", max_bytes=65_536)
        try:
            payload = json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ChartRegistryAuthRequiredError("the registry token endpoint returned invalid JSON") from exc
        token = payload.get("token") or payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise ChartRegistryAuthRequiredError("the registry token endpoint returned no anonymous token")
        return token

    def _send(self, url: str, headers: Mapping[str, str], max_bytes: int) -> _Response:
        try:
            with self._client.stream("GET", url, headers=dict(headers)) as response:
                length = response.headers.get("content-length")
                if length is not None and length.isdigit() and int(length) > max_bytes:
                    raise ChartFetchLimitError(f"{url} exceeds the {max_bytes}-byte download limit")
                body = bytearray()
                if response.status_code == 200:
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise ChartFetchLimitError(f"{url} exceeds the {max_bytes}-byte download limit")
                return _Response(response.status_code, response.headers, bytes(body))
        except httpx.HTTPError as exc:
            raise ChartFetchError(f"{url} could not be fetched ({type(exc).__name__})") from exc


@dataclass(frozen=True, slots=True)
class _Response:
    status_code: int
    headers: httpx.Headers
    content: bytes


def _normalize_repository(repository: str) -> str:
    value = repository.rstrip("/")
    lowered = value.lower()
    if lowered.startswith("oci://"):
        rest = value[len("oci://") :]
    elif "://" in value:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ChartReferenceError(f"chart repository {repository!r} must be an http(s) or oci URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ChartReferenceError("chart repository URLs must not carry credentials, a query or a fragment")
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    else:
        # Argo CD writes OCI Helm repositories without a scheme (registry/path).
        rest = value
    host, _, path = rest.partition("/")
    if not host or _HOST_PATTERN.fullmatch(host.lower()) is None or host.startswith("*"):
        raise ChartReferenceError(f"OCI chart repository {repository!r} has an invalid registry host")
    components = [part for part in path.split("/") if part]
    if any(_OCI_PATH_COMPONENT.fullmatch(part) is None for part in components):
        raise ChartReferenceError(f"OCI chart repository {repository!r} has an invalid path")
    return "oci://" + "/".join((host.lower(), *components))


def _split_oci(repository: str) -> tuple[str, str]:
    rest = repository[len("oci://") :]
    host, _, path = rest.partition("/")
    return host, path


def _normalize_host_patterns(values: Sequence[str]) -> list[str]:
    patterns: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("allowed chart hosts must be strings")
        value = raw.strip().lower().rstrip("/")
        if "://" in value:
            parts = urlsplit(value)
            value = parts.netloc
        if not value or _HOST_PATTERN.fullmatch(value) is None:
            raise ValueError(f"allowed chart host {raw!r} must be a host, host:port or *.domain pattern")
        patterns.append(value)
    return patterns


def _host_matches(pattern: str, host: str, host_port: str) -> bool:
    """``host``, ``host:port`` or ``*.domain`` (any subdomain, optionally with a port)."""
    candidate = host_port if ":" in pattern else host
    if pattern.startswith("*."):
        return fnmatch.fnmatchcase(candidate, pattern)
    return pattern == candidate


def _default_resolver(host: str) -> list[str]:
    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})
    except (OSError, UnicodeError):
        return []


def _reject_internal_host(host: str, resolver: Resolver) -> None:
    if host in _METADATA_HOSTNAMES or host.endswith(".localhost"):
        raise ChartHostNotAllowedError(f"chart host {host!r} is a local or metadata endpoint")
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host.strip("[]"))
        candidates.append(host.strip("[]"))
    except ValueError:
        candidates.extend(resolver(host))
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError:
            continue
        if address in _METADATA_ADDRESSES:
            raise ChartHostNotAllowedError(f"chart host {host!r} resolves to a cloud metadata address")
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or (address.version == 6 and getattr(address, "ipv4_mapped", None) is not None)
        ):
            raise ChartHostNotAllowedError(f"chart host {host!r} resolves to a local, link-local or metadata address")


def _verify_digest(content: bytes, expected: str, label: str) -> None:
    actual = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if actual != expected:
        raise ChartDigestMismatchError(f"{label} digest {actual} does not match the published digest {expected}")


def _index_entry(body: bytes, reference: ChartReference) -> Mapping[str, Any]:
    parser = YAML(typ="safe")
    try:
        document = parser.load(body.decode("utf-8"))
    except (UnicodeDecodeError, YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise ChartNotFoundError("the Helm repository index.yaml is not valid YAML") from exc
    entries = document.get("entries") if isinstance(document, Mapping) else None
    versions = entries.get(reference.name) if isinstance(entries, Mapping) else None
    if not isinstance(versions, list):
        raise ChartNotFoundError(f"chart {reference.name!r} is not published by {reference.repository}")
    wanted = {reference.version, reference.version.removeprefix("v"), f"v{reference.version.removeprefix('v')}"}
    matches = [item for item in versions if isinstance(item, Mapping) and str(item.get("version")) in wanted]
    if not matches:
        raise ChartNotFoundError(f"{reference.display} is not published by the repository index")
    if len(matches) != 1:
        raise ChartNotFoundError(f"the repository index lists {reference.display} more than once")
    return matches[0]


def _chart_layer_digest(manifest_bytes: bytes, reference: ChartReference) -> str:
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ChartArchiveError(f"{reference.display} manifest is not valid JSON") from exc
    if not isinstance(manifest, Mapping):
        raise ChartArchiveError(f"{reference.display} manifest must be a JSON object")
    if manifest.get("mediaType") in _INDEX_MEDIA_TYPES or "manifests" in manifest:
        raise ChartArchiveError(f"{reference.display} is an image index, not a Helm chart artifact")
    config = manifest.get("config")
    if not isinstance(config, Mapping) or config.get("mediaType") != HELM_CONFIG_MEDIA_TYPE:
        raise ChartArchiveError(f"{reference.display} is not a Helm chart artifact (unexpected config media type)")
    layers = manifest.get("layers")
    chart_layers = [
        layer
        for layer in (layers if isinstance(layers, list) else [])
        if isinstance(layer, Mapping) and layer.get("mediaType") in HELM_CHART_MEDIA_TYPES
    ]
    if len(chart_layers) != 1:
        raise ChartArchiveError(f"{reference.display} must contain exactly one Helm chart layer")
    digest = chart_layers[0].get("digest")
    size = chart_layers[0].get("size")
    if not isinstance(digest, str) or SHA256_DIGEST.fullmatch(digest) is None:
        raise ChartArchiveError(f"{reference.display} chart layer digest is not a sha256 digest")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ChartArchiveError(f"{reference.display} chart layer size is invalid")
    return digest


def _unpack_chart(
    reference: ChartReference,
    digest: str,
    archive_digest: str,
    archive: bytes,
    limits: ChartFetchLimits,
) -> FetchedChart:
    budget = _UnpackBudget(limits)
    files = _unpack_archive(archive, budget, depth=0)
    metadata_content = files.get("Chart.yaml")
    if metadata_content is None:
        raise ChartArchiveError(f"{reference.display} archive has no Chart.yaml")
    metadata = _chart_metadata(metadata_content, reference.display)
    name = metadata.get("name")
    version = metadata.get("version")
    if not isinstance(name, str) or name != reference.chart_name:
        raise ChartArchiveError(f"{reference.display} archive declares chart name {name!r}")
    if not isinstance(version, str) or version.removeprefix("v") != reference.version.removeprefix("v"):
        raise ChartArchiveError(f"{reference.display} archive declares chart version {version!r}")
    ordered = dict(sorted(files.items()))
    return FetchedChart(
        reference=reference,
        name=name,
        version=version,
        digest=digest,
        archive_digest=archive_digest,
        files=MappingProxyType(ordered),
    )


@dataclass(slots=True)
class _UnpackBudget:
    limits: ChartFetchLimits
    files: int = 0
    total_bytes: int = 0

    def charge(self, size: int) -> None:
        self.files += 1
        self.total_bytes += size
        if size > self.limits.max_file_bytes:
            raise ChartFetchLimitError(f"a chart file exceeds the {self.limits.max_file_bytes}-byte limit")
        if self.files > self.limits.max_files:
            raise ChartFetchLimitError(f"the chart exceeds the {self.limits.max_files}-file limit")
        if self.total_bytes > self.limits.max_total_bytes:
            raise ChartFetchLimitError(f"the chart exceeds the {self.limits.max_total_bytes}-byte unpacked limit")


def _unpack_archive(archive: bytes, budget: _UnpackBudget, *, depth: int) -> dict[str, bytes]:
    if depth > _MAX_NESTED_CHART_DEPTH:
        raise ChartArchiveError("packaged chart dependencies are nested too deeply")
    try:
        decompressed = _bounded_gunzip(archive, budget.limits.max_total_bytes + 1_048_576)
        tar = tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ChartArchiveError("the chart archive is not a valid gzip-compressed tar file") from exc
    files: dict[str, bytes] = {}
    top_level: str | None = None
    with tar:
        for member in tar:
            if member.isdir():
                continue
            if not member.isreg():
                raise ChartArchiveError("chart archives must contain only regular files and directories")
            parts = _safe_member_parts(member.name)
            if top_level is None:
                top_level = parts[0]
            elif parts[0] != top_level:
                raise ChartArchiveError("a chart archive must contain exactly one top-level chart directory")
            if len(parts) < 2:
                raise ChartArchiveError("chart archive files must be inside the chart directory")
            relative = "/".join(parts[1:])
            budget.charge(member.size)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise ChartArchiveError("a chart archive member could not be read")
            content = extracted.read(budget.limits.max_file_bytes + 1)
            if len(content) != member.size:
                raise ChartArchiveError("a chart archive member is truncated")
            if relative in files:
                raise ChartArchiveError(f"the chart archive contains {relative!r} twice")
            files[relative] = content
    if top_level is None:
        raise ChartArchiveError("the chart archive is empty")
    return _expand_packaged_dependencies(files, budget, depth=depth)


def _expand_packaged_dependencies(files: dict[str, bytes], budget: _UnpackBudget, *, depth: int) -> dict[str, bytes]:
    """Unpack ``charts/<dependency>.tgz`` into ``charts/<dependency>/`` (equivalent for Helm)."""
    expanded: dict[str, bytes] = {}
    packaged: list[tuple[str, bytes]] = []
    for path, content in files.items():
        parts = PurePosixPath(path).parts
        if len(parts) == 2 and parts[0] == "charts" and path.endswith(".tgz"):
            packaged.append((path, content))
        else:
            expanded[path] = content
    for path, content in packaged:
        dependency_files = _unpack_archive(content, budget, depth=depth + 1)
        metadata = _chart_metadata(dependency_files.get("Chart.yaml"), path)
        name = metadata.get("name")
        if not isinstance(name, str) or _CHART_NAME.fullmatch(name) is None:
            raise ChartArchiveError(f"packaged dependency {path} declares an invalid chart name")
        for relative, dependency_content in dependency_files.items():
            target = f"charts/{name}/{relative}"
            if target in expanded:
                raise ChartArchiveError(f"packaged dependency {path} collides with {target}")
            expanded[target] = dependency_content
    return expanded


def _bounded_gunzip(archive: bytes, max_bytes: int) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(archive)) as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ChartFetchLimitError(f"the chart archive expands beyond {max_bytes} bytes")
    return data


def _safe_member_parts(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise ChartArchiveError(f"chart archive path {name!r} is unsafe")
    parts = tuple(part for part in name.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise ChartArchiveError(f"chart archive path {name!r} is unsafe")
    if parts[0].startswith("-"):
        raise ChartArchiveError(f"chart archive path {name!r} is unsafe")
    return parts


def _chart_metadata(content: bytes | None, label: str) -> Mapping[str, Any]:
    if content is None:
        raise ChartArchiveError(f"{label} has no Chart.yaml")
    parser = YAML(typ="safe", pure=True)
    try:
        value = parser.load(content.decode("utf-8"))
    except (UnicodeDecodeError, YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise ChartArchiveError(f"{label} Chart.yaml is invalid YAML") from exc
    if not isinstance(value, Mapping):
        raise ChartArchiveError(f"{label} Chart.yaml must be a mapping")
    return value
