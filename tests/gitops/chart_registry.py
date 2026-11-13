"""Recorded chart fixtures served by in-memory OCI and HTTP Helm repositories."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

CHART_FIXTURES = Path(__file__).parent / "fixtures" / "charts"
OCI_REGISTRY = "registry-1.docker.io"
OCI_REPOSITORY = "bitnamicharts/nginx"
OCI_TOKEN_REALM = "https://auth.docker.io/token"
OCI_BLOB_HOST = "production.cloudflare.docker.com"
HTTP_REPOSITORY = "https://charts.bitnami.com/bitnami"
ANONYMOUS_TOKEN = "anonymous-pull-token"


def chart_files(name: str = "nginx") -> dict[str, bytes]:
    root = CHART_FIXTURES / name
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def package_chart(files: Mapping[str, bytes], *, top_level: str = "nginx") -> bytes:
    """A deterministic ``helm package``-style archive: one top-level chart directory."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for relative, content in sorted(files.items()):
            entry = tarfile.TarInfo(f"{top_level}/{relative}")
            entry.size = len(content)
            entry.mode = 0o644
            entry.mtime = 0
            archive.addfile(entry, io.BytesIO(content))
    return gzip.compress(raw.getvalue(), mtime=0)


def sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def oci_manifest(archive: bytes) -> bytes:
    config = b"{}"
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.cncf.helm.config.v1+json",
                "digest": sha256(config),
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
                    "digest": sha256(archive),
                    "size": len(archive),
                }
            ],
        },
        sort_keys=True,
    ).encode()


@dataclass
class FakeChartRegistry:
    """Docker Hub-like OCI registry (anonymous bearer token, blob redirect) and an HTTP repository."""

    version: str = "18.3.1"
    archive: bytes = b""
    served_archive: bytes | None = None
    index_digest: str | None = None
    token_status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.archive:
            self.archive = package_chart(chart_files())

    @property
    def manifest(self) -> bytes:
        return oci_manifest(self.archive)

    @property
    def manifest_digest(self) -> str:
        return sha256(self.manifest)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, host: str, path_prefix: str = "") -> int:
        return sum(
            1 for request in self.requests if request.url.host == host and request.url.path.startswith(path_prefix)
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        path = request.url.path
        served = self.served_archive if self.served_archive is not None else self.archive
        if host == "auth.docker.io" and path == "/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status)
            assert request.url.params.get("scope") == f"repository:{OCI_REPOSITORY}:pull"
            return httpx.Response(200, json={"token": ANONYMOUS_TOKEN})
        if host == OCI_REGISTRY:
            if request.headers.get("authorization") != f"Bearer {ANONYMOUS_TOKEN}":
                return httpx.Response(
                    401,
                    headers={
                        "www-authenticate": (
                            f'Bearer realm="{OCI_TOKEN_REALM}",service="registry.docker.io",'
                            f'scope="repository:{OCI_REPOSITORY}:pull"'
                        )
                    },
                )
            if path == f"/v2/{OCI_REPOSITORY}/manifests/{self.version}":
                return httpx.Response(
                    200,
                    content=self.manifest,
                    headers={
                        "content-type": "application/vnd.oci.image.manifest.v1+json",
                        "docker-content-digest": self.manifest_digest,
                    },
                )
            if path == f"/v2/{OCI_REPOSITORY}/blobs/{sha256(self.archive)}":
                return httpx.Response(307, headers={"location": f"https://{OCI_BLOB_HOST}/blobs/chart.tgz"})
            return httpx.Response(404)
        if host == OCI_BLOB_HOST and path == "/blobs/chart.tgz":
            assert "authorization" not in request.headers, "registry tokens must not follow cross-host redirects"
            return httpx.Response(200, content=served)
        if host == "charts.bitnami.com":
            if path == "/bitnami/index.yaml":
                digest = self.index_digest or hashlib.sha256(self.archive).hexdigest()
                index = (
                    "apiVersion: v1\n"
                    "entries:\n"
                    "  nginx:\n"
                    f"    - name: nginx\n      version: {self.version}\n      digest: {digest}\n"
                    f"      urls:\n        - nginx-{self.version}.tgz\n"
                    "    - name: nginx\n      version: 18.3.0\n      digest: 00\n"
                    "      urls:\n        - nginx-18.3.0.tgz\n"
                )
                return httpx.Response(200, text=index)
            if path == f"/bitnami/nginx-{self.version}.tgz":
                return httpx.Response(200, content=served)
        return httpx.Response(404)


def public_resolver(host: str) -> list[str]:
    del host
    return ["93.184.216.34"]
