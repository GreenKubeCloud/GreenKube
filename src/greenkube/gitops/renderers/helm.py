"""Bounded rendering of in-snapshot Helm charts."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from greenkube.gitops.contracts import RendererKind
from greenkube.gitops.renderers.raw import RawRenderError, RawRenderResult, RenderLimits, render_raw
from greenkube.gitops.renderers.sandbox import (
    SandboxCommandNotFoundError,
    SandboxError,
    SandboxIsolationUnavailableError,
    SandboxOutputLimitError,
    SandboxPathError,
    SandboxResult,
    SandboxRunner,
    SandboxTimeoutError,
)
from greenkube.gitops.snapshots.models import RepositorySnapshot

_DEFAULT_KUBERNETES_VERSION = "1.30.0"
_VERSION_MAX_BYTES = 4_096
_HELM_VERSION = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_KUBERNETES_VERSION = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_API_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*){0,2}$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_REMOTE_HOST = re.compile(r"^(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+(?::[0-9]+)?/")
_TEMPLATE_ACTION = re.compile(r"{{-?(.*?)-?}}", re.DOTALL)
_TEMPLATE_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HELM_SOURCE_LINE = re.compile(r"^# Source: ([^\r\n]+)$")
_YAML_DOCUMENT_SEPARATOR = re.compile(r"^---(?:[ \t]+#.*)?[ \t]*$")
_UNVALIDATED_TEMPLATE_FUNCTIONS = frozenset(
    {
        "getHostByName",
        "genCA",
        "genCAWithKey",
        "genPrivateKey",
        "genSelfSignedCert",
        "genSelfSignedCertWithKey",
        "genSignedCert",
        "genSignedCertWithKey",
        "htpasswd",
        "lookup",
        "now",
        "randAlpha",
        "randAlphaNum",
        "randAscii",
        "randBytes",
        "randInt",
        "randNumeric",
        "uuidv4",
    }
)


@dataclass(frozen=True, slots=True)
class HelmLimits:
    """Hard limits applied to a Helm render and its in-snapshot inputs."""

    timeout_seconds: float = 30.0
    max_output_bytes: int = 4_194_304
    max_stderr_bytes: int = 65_536
    max_chart_files: int = 4_096
    max_chart_file_bytes: int = 2_097_152
    max_total_chart_bytes: int = 33_554_432
    max_values_files: int = 32
    max_total_values_bytes: int = 8_388_608

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        for name, value in (
            ("max_output_bytes", self.max_output_bytes),
            ("max_stderr_bytes", self.max_stderr_bytes),
            ("max_chart_files", self.max_chart_files),
            ("max_chart_file_bytes", self.max_chart_file_bytes),
            ("max_total_chart_bytes", self.max_total_chart_bytes),
            ("max_values_files", self.max_values_files),
            ("max_total_values_bytes", self.max_total_values_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class HelmVersionMetadata:
    """Observed Helm version and the required renderer pin."""

    binary_name: str
    version: str
    expected_version: str

    @property
    def is_pinned(self) -> bool:
        return self.version == self.expected_version


class HelmError(RuntimeError):
    """Base error for invalid Helm input or rendering failures."""


class HelmPathError(HelmError):
    """A chart or values path is invalid or escapes the snapshot."""


class HelmConfigurationError(HelmError):
    """A chart or its render inputs are malformed or unsupported."""


class HelmRemoteReferenceError(HelmConfigurationError):
    """A remote chart, chart dependency, or values file was rejected."""


class HelmUnvalidatedError(HelmError):
    """A chart could not be rendered deterministically without cluster state."""


class HelmClusterDependentError(HelmUnvalidatedError):
    """A chart uses cluster-dependent or nondeterministic template functions."""


class HelmLimitError(HelmError):
    """The Helm chart or render output exceeded a configured limit."""


class HelmVersionError(HelmError):
    """Helm did not report a usable version."""


class HelmVersionMismatchError(HelmVersionError):
    """The Helm executable did not match the configured version pin."""


class HelmUnavailableError(HelmError):
    """The pinned Helm executable is unavailable."""


class HelmExecutionError(HelmError):
    """Helm could not be started or safely completed."""


class HelmIsolationUnavailableError(HelmExecutionError):
    """Helm execution was refused because an isolated renderer is not configured."""


class HelmTimeoutError(HelmError):
    """The Helm process exceeded its wall-clock timeout."""


class HelmOutputLimitError(HelmLimitError):
    """Helm exceeded a configured stdout or stderr byte limit."""

    def __init__(self, stream: str, limit_bytes: int) -> None:
        self.stream = stream
        self.limit_bytes = limit_bytes
        super().__init__(f"Helm {stream} output limit of {limit_bytes} bytes exceeded")


class HelmTemplateError(HelmExecutionError):
    """Helm returned a non-zero status while rendering a chart."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        super().__init__(f"Helm template failed with exit status {returncode}")


class HelmOutputError(HelmError):
    """Helm output could not be normalized as Kubernetes resources."""


class _Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> SandboxResult: ...


@dataclass(frozen=True, slots=True)
class _Chart:
    directory: Path
    metadata_file: Path
    name: str
    source_path: str
    files: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _ValuesFile:
    path: Path
    relative_path: str
    content: bytes


class HelmRenderer:
    """Render a local chart with pinned Helm and normalize the result via the raw renderer."""

    def __init__(
        self,
        *,
        expected_version: str,
        binary: str = "helm",
        limits: HelmLimits | None = None,
        raw_limits: RenderLimits | None = None,
        runner: _Runner | None = None,
        kube_version: str | None = None,
        api_versions: Sequence[str] | None = None,
    ) -> None:
        if (
            not isinstance(expected_version, str)
            or not expected_version
            or expected_version != expected_version.strip()
            or _HELM_VERSION.fullmatch(expected_version) is None
        ):
            raise ValueError("expected_version must be a non-empty, valid, trimmed Helm version")
        if not isinstance(binary, str) or not binary or "\x00" in binary:
            raise ValueError("binary must be a non-empty executable name or path")
        self.expected_version = expected_version
        self.binary = binary
        self.limits = limits or HelmLimits()
        self.raw_limits = raw_limits or RenderLimits()
        configured_kube_version = kube_version if kube_version is not None else os.getenv("GITOPS_HELM_KUBE_VERSION")
        self.kube_version = _validate_kubernetes_version(configured_kube_version or _DEFAULT_KUBERNETES_VERSION)
        self.api_versions = _configured_api_versions(api_versions)
        self._runner = runner or SandboxRunner()
        self._version_metadata: HelmVersionMetadata | None = None

    @property
    def version_metadata(self) -> HelmVersionMetadata | None:
        """Return the latest successful version preflight metadata, if available."""
        return self._version_metadata

    def _ensure_execution_backend(self) -> None:
        if isinstance(self._runner, SandboxRunner):
            try:
                self._runner.ensure_isolated()
            except SandboxIsolationUnavailableError as exc:
                raise HelmIsolationUnavailableError(
                    "Helm rendering is disabled because no isolated renderer backend is configured"
                ) from exc

    def preflight(self, snapshot: RepositorySnapshot) -> HelmVersionMetadata:
        """Verify that the executable in the snapshot workspace matches its required version pin."""
        self._ensure_execution_backend()
        workspace = _snapshot_workspace(snapshot)
        result = self._run((self.binary, "version", "--short"), workspace)
        if result.returncode != 0:
            raise HelmVersionError("Helm version command failed")
        if not result.stdout or len(result.stdout) > _VERSION_MAX_BYTES:
            raise HelmVersionError("Helm version output is empty or invalid")
        try:
            lines = result.stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise HelmVersionError("Helm version output is not valid UTF-8") from exc
        version = next((line.strip() for line in lines if line.strip()), "")
        if not version or "\x00" in version or _HELM_VERSION.fullmatch(version) is None:
            raise HelmVersionError("Helm version output is empty or invalid")

        metadata = HelmVersionMetadata(
            binary_name=Path(self.binary).name,
            version=version,
            expected_version=self.expected_version,
        )
        self._version_metadata = metadata
        if not metadata.is_pinned:
            raise HelmVersionMismatchError(
                f"Helm version {metadata.version!r} does not match expected pin {metadata.expected_version!r}"
            )
        return metadata

    def render(
        self,
        snapshot: RepositorySnapshot,
        chart_path: str | Path,
        *,
        release_name: str,
        namespace: str | None = None,
        release_namespace: str | None = None,
        values_files: Sequence[str | Path] = (),
        kube_version: str | None = None,
        api_versions: Sequence[str] | None = None,
    ) -> RawRenderResult:
        """Render an in-repository chart and ordered values files from a snapshot."""
        self._ensure_execution_backend()
        workspace = _snapshot_workspace(snapshot)
        chart = _inspect_chart(workspace, chart_path, self.limits)
        prepared_values = _inspect_values_files(workspace, values_files, self.limits)
        if namespace is not None and release_namespace is not None and namespace != release_namespace:
            raise HelmConfigurationError("namespace and release_namespace must agree when both are supplied")
        selected_namespace = release_namespace if release_namespace is not None else namespace
        if selected_namespace is None:
            raise HelmConfigurationError("a Helm release namespace is required")
        _validate_release_identity(release_name, selected_namespace)
        selected_kube_version = (
            self.kube_version if kube_version is None else _validate_kubernetes_version(kube_version)
        )
        selected_api_versions = self.api_versions if api_versions is None else _validate_api_versions(api_versions)
        _validate_deterministic_templates(chart, prepared_values, self.limits)
        self.preflight(snapshot)

        arguments = [
            self.binary,
            "template",
            release_name,
            chart.directory.relative_to(workspace).as_posix() or ".",
            "--namespace",
            selected_namespace,
            "--kube-version",
            selected_kube_version,
            "--no-hooks",
        ]
        for api_version in selected_api_versions:
            arguments.extend(("--api-versions", api_version))
        for values_file in prepared_values:
            arguments.extend(("--values", values_file.relative_path))

        process_result = self._run(arguments, workspace)
        if process_result.returncode != 0:
            raise HelmTemplateError(process_result.returncode)
        if len(process_result.stdout) > self.limits.max_output_bytes:
            raise HelmOutputLimitError("stdout", self.limits.max_output_bytes)
        if len(process_result.stderr) > self.limits.max_stderr_bytes:
            raise HelmOutputLimitError("stderr", self.limits.max_stderr_bytes)

        try:
            raw_result = render_raw({chart.source_path: process_result.stdout}, limits=self.raw_limits)
        except RawRenderError as exc:
            raise HelmOutputError("Helm output is not a valid bounded Kubernetes manifest set") from exc
        source_paths = _helm_source_map(process_result.stdout, workspace, chart)
        resources = tuple(
            replace(resource, source_path=source_paths.get(resource.document_index, chart.source_path))
            for resource in raw_result.resources
        )
        return replace(raw_result, renderer=RendererKind.HELM_RELEASE, resources=resources)

    def _run(self, argv: Sequence[str], workspace: Path) -> SandboxResult:
        try:
            result = self._runner.run(
                argv,
                workspace=workspace,
                cwd=workspace,
                timeout_seconds=self.limits.timeout_seconds,
                max_stdout_bytes=self.limits.max_output_bytes,
                max_stderr_bytes=self.limits.max_stderr_bytes,
            )
        except (SandboxCommandNotFoundError, FileNotFoundError) as exc:
            raise HelmUnavailableError("the pinned Helm executable is unavailable") from exc
        except (SandboxTimeoutError, TimeoutError) as exc:
            raise HelmTimeoutError("Helm render timed out at its configured limit") from exc
        except SandboxOutputLimitError as exc:
            raise HelmOutputLimitError(exc.stream, exc.limit_bytes) from exc
        except SandboxPathError as exc:
            raise HelmPathError("Helm process paths must remain inside the snapshot workspace") from exc
        except SandboxIsolationUnavailableError as exc:
            raise HelmIsolationUnavailableError(
                "Helm rendering is disabled because no isolated renderer backend is configured"
            ) from exc
        except SandboxError as exc:
            raise HelmExecutionError("Helm sandbox execution failed") from exc
        except OSError as exc:
            raise HelmExecutionError("Helm sandbox execution failed") from exc

        if not isinstance(result, SandboxResult):
            raise HelmExecutionError("Helm runner returned an invalid process result")
        if (
            type(result.returncode) is not int
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
        ):
            raise HelmExecutionError("Helm runner returned an invalid process result")
        if len(result.stdout) > self.limits.max_output_bytes:
            raise HelmOutputLimitError("stdout", self.limits.max_output_bytes)
        if len(result.stderr) > self.limits.max_stderr_bytes:
            raise HelmOutputLimitError("stderr", self.limits.max_stderr_bytes)
        return result


def _snapshot_workspace(snapshot: RepositorySnapshot) -> Path:
    try:
        workspace = Path(snapshot.workspace)
        if workspace.is_symlink():
            raise HelmPathError("snapshot workspace must not be a symbolic link")
        root = workspace.resolve(strict=True)
    except HelmPathError:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HelmPathError("snapshot workspace must be an existing directory") from exc
    if not root.is_dir():
        raise HelmPathError("snapshot workspace must be an existing directory")
    return root


def _inspect_chart(root: Path, chart_path: str | Path, limits: HelmLimits) -> _Chart:
    chart_directory = _resolve_snapshot_path(
        root,
        chart_path,
        kind="chart",
        allow_root=True,
        required=True,
    )
    if not chart_directory.is_dir():
        raise HelmPathError("Helm chart path must identify a directory inside the snapshot")

    files = _scan_chart_files(root, chart_directory, limits)
    if any(path.suffix == ".tgz" and "charts" in path.relative_to(chart_directory).parts for path in files):
        raise HelmUnvalidatedError("packaged Helm dependencies cannot be inspected as in-snapshot templates")
    metadata_file = chart_directory / "Chart.yaml"
    if metadata_file not in files:
        raise HelmConfigurationError("Helm chart directory must contain a regular Chart.yaml file")

    root_metadata: Mapping[str, Any] | None = None
    for path in files:
        if path.name not in {"Chart.yaml", "Chart.lock", "requirements.yaml"}:
            continue
        mapping = _load_yaml_mapping(
            _read_bounded_file(path, limits.max_chart_file_bytes, category="Helm chart"),
            path,
            category="Helm chart",
        )
        if path.name in {"Chart.yaml", "requirements.yaml"}:
            _validate_chart_dependencies(root, path.parent, mapping)
            if path == metadata_file:
                root_metadata = mapping
        elif path.name == "Chart.lock":
            _validate_locked_dependencies(root, path.parent, mapping, path)
    _validate_values_schemas(root, chart_directory, files, limits)
    chart_name_value = None if root_metadata is None else root_metadata.get("name")
    if not isinstance(chart_name_value, str) or not chart_name_value or chart_name_value != chart_name_value.strip():
        raise HelmConfigurationError("Helm Chart.yaml must declare a non-empty chart name")

    source_path = metadata_file.relative_to(root).as_posix()
    return _Chart(chart_directory, metadata_file, chart_name_value, source_path, tuple(files))


def _scan_chart_files(root: Path, chart_directory: Path, limits: HelmLimits) -> list[Path]:
    files: list[Path] = []
    pending = [chart_directory]
    total_bytes = 0

    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                ordered_entries = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise HelmPathError("Helm chart directory could not be read inside the snapshot") from exc

        for entry in ordered_entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    raise HelmPathError("Helm chart entries must not be symbolic links")
                if entry.is_dir(follow_symlinks=False):
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(root):
                        raise HelmPathError("Helm chart path escapes the snapshot workspace")
                    pending.append(resolved)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise HelmPathError("Helm charts must contain only regular files and directories")
                info = entry.stat(follow_symlinks=False)
            except HelmPathError:
                raise
            except OSError as exc:
                raise HelmPathError("Helm chart entry could not be inspected safely") from exc

            if not stat.S_ISREG(info.st_mode):
                raise HelmPathError("Helm charts must contain only regular files and directories")
            if info.st_size > limits.max_chart_file_bytes:
                raise HelmLimitError(f"Helm chart file byte limit of {limits.max_chart_file_bytes} exceeded")
            if len(files) >= limits.max_chart_files:
                raise HelmLimitError(f"Helm chart file limit of {limits.max_chart_files} exceeded")
            total_bytes += info.st_size
            if total_bytes > limits.max_total_chart_bytes:
                raise HelmLimitError(f"Helm total chart byte limit of {limits.max_total_chart_bytes} exceeded")

            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise HelmPathError("Helm chart path escapes the snapshot workspace")
            files.append(resolved)

    files.sort(key=lambda path: path.relative_to(chart_directory).as_posix())
    return files


def _inspect_values_files(
    root: Path,
    values_files: Sequence[str | Path],
    limits: HelmLimits,
) -> tuple[_ValuesFile, ...]:
    if not isinstance(values_files, Sequence) or isinstance(values_files, (str, bytes)):
        raise HelmConfigurationError("values_files must be an ordered sequence of repository-relative paths")
    if len(values_files) > limits.max_values_files:
        raise HelmLimitError(f"Helm values file limit of {limits.max_values_files} exceeded")

    prepared: list[_ValuesFile] = []
    total_bytes = 0
    for reference in values_files:
        path = _resolve_snapshot_path(
            root,
            reference,
            kind="values",
            allow_root=False,
            required=True,
        )
        if not path.is_file():
            raise HelmPathError("Helm values path must identify a regular file inside the snapshot")
        content = _read_bounded_file(path, limits.max_chart_file_bytes, category="Helm values")
        total_bytes += len(content)
        if total_bytes > limits.max_total_values_bytes:
            raise HelmLimitError(f"Helm total values byte limit of {limits.max_total_values_bytes} exceeded")
        prepared.append(
            _ValuesFile(
                path=path,
                relative_path=path.relative_to(root).as_posix(),
                content=content,
            )
        )
    return tuple(prepared)


def _resolve_snapshot_path(
    root: Path,
    reference: str | Path,
    *,
    kind: str,
    allow_root: bool,
    required: bool,
    base: Path | None = None,
) -> Path:
    if not isinstance(reference, (str, Path)):
        raise HelmPathError(f"Helm {kind} path must be a repository-relative POSIX path")
    value = str(reference)
    if not value or value != value.strip() or "\x00" in value or "\\" in value:
        raise HelmPathError(f"Helm {kind} path must be a non-empty relative POSIX path")
    if value.startswith("/") or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise HelmPathError(f"Helm {kind} path must not be absolute")
    if _is_remote_reference(value):
        raise HelmRemoteReferenceError(f"remote Helm {kind} references are disabled")
    if any(part == "" for part in value.split("/")):
        raise HelmPathError(f"Helm {kind} path must not contain empty components")

    parts = PurePosixPath(value).parts
    if parts and parts[0].startswith("-"):
        raise HelmPathError(f"Helm {kind} path must not begin with an option marker")
    if ".." in parts:
        raise HelmPathError(f"Helm {kind} path must not traverse outside the snapshot")
    path_base = root if base is None else base
    if not path_base.is_relative_to(root):
        raise HelmPathError(f"Helm {kind} path base must remain inside the snapshot workspace")
    if not parts or parts == (".",):
        if allow_root:
            return path_base
        raise HelmPathError(f"Helm {kind} path must identify a file inside the snapshot")

    current = path_base
    for part in parts:
        if part == ".":
            continue
        current = current / part
        if current.is_symlink():
            raise HelmPathError(f"Helm {kind} path must not traverse symbolic links")
    try:
        resolved = current.resolve(strict=required)
    except (OSError, RuntimeError) as exc:
        raise HelmPathError(f"Helm {kind} path does not exist inside the snapshot") from exc
    if not resolved.is_relative_to(root):
        raise HelmPathError(f"Helm {kind} path escapes the snapshot workspace")
    if required and not resolved.exists():
        raise HelmPathError(f"Helm {kind} path does not exist inside the snapshot")
    return resolved


def _is_remote_reference(value: str) -> bool:
    if value.lower().startswith("git@"):
        return True
    if _REMOTE_HOST.match(value) is not None:
        return True
    if value.startswith("//"):
        return True
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value))


def _read_bounded_file(path: Path, max_bytes: int, *, category: str) -> bytes:
    try:
        with path.open("rb") as source:
            content = source.read(max_bytes + 1)
    except OSError as exc:
        raise HelmPathError(f"{category} file could not be read inside the snapshot") from exc
    if len(content) > max_bytes:
        raise HelmLimitError(f"{category} file byte limit of {max_bytes} exceeded")
    return content


def _load_yaml_mapping(content: bytes, path: Path, *, category: str) -> Mapping[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HelmConfigurationError(f"{category} {path.name} must be valid UTF-8") from exc

    parser = YAML(typ="safe", pure=True)
    parser.version = (1, 2)
    parser.allow_duplicate_keys = False
    try:
        value = parser.load(text)
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise HelmConfigurationError(f"{category} {path.name} is invalid YAML ({type(exc).__name__})") from exc
    if not isinstance(value, Mapping):
        raise HelmConfigurationError(f"{category} {path.name} must contain a mapping")
    return value


def _validate_chart_dependencies(root: Path, chart_directory: Path, config: Mapping[str, Any]) -> None:
    dependencies = config.get("dependencies", [])
    if dependencies is None:
        return
    if not isinstance(dependencies, list):
        raise HelmConfigurationError("Helm chart dependencies must be a list")

    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            raise HelmConfigurationError("Helm chart dependencies must be mappings")
        repository = dependency.get("repository", "")
        if repository is None:
            repository = ""
        if not isinstance(repository, str) or repository != repository.strip():
            raise HelmConfigurationError("Helm chart dependency repository must be a string")
        if not repository:
            continue
        if not repository.startswith("file://"):
            raise HelmRemoteReferenceError("remote Helm chart dependencies are disabled")

        local_reference = repository.removeprefix("file://")
        _validate_local_chart_dependency(root, chart_directory, local_reference)


def _validate_locked_dependencies(
    root: Path,
    chart_directory: Path,
    config: Mapping[str, Any],
    source: Path,
) -> None:
    dependencies = config.get("dependencies", [])
    if dependencies is None:
        return
    if not isinstance(dependencies, list):
        raise HelmConfigurationError(f"{source.name}: locked Helm dependencies must be a list")
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            raise HelmConfigurationError(f"{source.name}: locked Helm dependencies must be mappings")
        repository = dependency.get("repository", "")
        if repository is None or repository == "":
            continue
        if not isinstance(repository, str):
            raise HelmConfigurationError(f"{source.name}: dependency repository must be a string")
        if not repository.startswith("file://"):
            raise HelmRemoteReferenceError("remote Helm chart dependencies are disabled")
        _validate_local_chart_dependency(root, chart_directory, repository.removeprefix("file://"))


def _validate_values_schemas(
    root: Path,
    chart_directory: Path,
    chart_files: list[Path],
    limits: HelmLimits,
) -> None:
    pending = [path for path in chart_files if path.name == "values.schema.json"]
    visited: set[Path] = set()
    total_bytes = 0

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    while pending:
        schema_path = pending.pop()
        if schema_path in visited:
            continue
        if len(visited) >= limits.max_chart_files:
            raise HelmLimitError(f"Helm schema file limit of {limits.max_chart_files} exceeded")
        visited.add(schema_path)

        content = _read_bounded_file(
            schema_path,
            limits.max_chart_file_bytes,
            category="Helm values schema",
        )
        total_bytes += len(content)
        if total_bytes > limits.max_total_chart_bytes:
            raise HelmLimitError(f"Helm total schema byte limit of {limits.max_total_chart_bytes} exceeded")
        try:
            document = json.loads(content.decode("utf-8"), object_pairs_hook=unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise HelmConfigurationError(f"Helm values schema {schema_path.name} is invalid JSON") from exc
        if not isinstance(document, Mapping):
            raise HelmConfigurationError(f"Helm values schema {schema_path.name} must contain an object")

        nodes: list[Any] = [document]
        while nodes:
            node = nodes.pop()
            if isinstance(node, Mapping):
                for reference_key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    if reference_key in node:
                        reference = node[reference_key]
                        if not isinstance(reference, str):
                            raise HelmConfigurationError(f"Helm values schema {reference_key} values must be strings")
                        try:
                            parsed_reference = urlsplit(reference)
                        except ValueError as exc:
                            raise HelmConfigurationError("Helm values schema reference is malformed") from exc
                        if parsed_reference.scheme or parsed_reference.netloc or _is_remote_reference(reference):
                            raise HelmRemoteReferenceError("remote Helm values schema references are disabled")
                        if parsed_reference.query:
                            raise HelmConfigurationError("Helm values schema references must not contain queries")
                        if parsed_reference.path:
                            referenced_schema = _resolve_snapshot_path(
                                root,
                                parsed_reference.path,
                                kind="values schema",
                                allow_root=False,
                                required=True,
                                base=schema_path.parent,
                            )
                            if not referenced_schema.is_file():
                                raise HelmPathError("Helm values schema reference must identify a file")
                            pending.append(referenced_schema)
                nodes.extend(node.values())
            elif isinstance(node, list):
                nodes.extend(node)


def _validate_local_chart_dependency(root: Path, chart_directory: Path, reference: str) -> None:
    local_chart = _resolve_snapshot_path(
        root,
        reference,
        kind="local chart dependency",
        allow_root=False,
        required=True,
        base=chart_directory,
    )
    metadata_file = local_chart / "Chart.yaml"
    if not local_chart.is_dir() or metadata_file.is_symlink() or not metadata_file.is_file():
        raise HelmPathError("local Helm chart dependency must be a chart directory inside the snapshot")


def _validate_deterministic_templates(
    chart: _Chart,
    values_files: tuple[_ValuesFile, ...],
    limits: HelmLimits,
) -> None:
    template_files = tuple(
        path for path in chart.files if "templates" in path.relative_to(chart.directory).parts or path.suffix == ".tpl"
    )
    uses_tpl = False
    for path in template_files:
        content = _read_bounded_file(path, limits.max_chart_file_bytes, category="Helm template")
        text = _decode_template(content, path)
        unsafe_functions, has_tpl = _unvalidated_functions(text)
        uses_tpl = uses_tpl or has_tpl
        if unsafe_functions:
            raise HelmClusterDependentError(
                f"Helm templates use cluster-dependent or nondeterministic functions ({', '.join(unsafe_functions)})"
            )

    if not uses_tpl:
        return

    scanned_paths = set(template_files)
    for path in chart.files:
        if path in scanned_paths:
            continue
        content = _read_bounded_file(path, limits.max_chart_file_bytes, category="Helm chart")
        text = content.decode("utf-8", errors="ignore")
        unsafe_functions, _ = _unvalidated_functions(text)
        if unsafe_functions:
            raise HelmClusterDependentError(
                f"Helm tpl input uses cluster-dependent or nondeterministic functions ({', '.join(unsafe_functions)})"
            )

    for values_file in values_files:
        text = values_file.content.decode("utf-8", errors="ignore")
        unsafe_functions, _ = _unvalidated_functions(text)
        if unsafe_functions:
            raise HelmClusterDependentError(
                "Helm values used by tpl contain cluster-dependent or nondeterministic functions "
                f"({', '.join(unsafe_functions)})"
            )


def _helm_source_map(output: bytes, root: Path, chart: _Chart) -> dict[int, str]:
    text = output.decode("utf-8")
    source_paths: dict[int, str] = {}
    source_path = chart.source_path
    document_index = 0
    has_document = False

    for line in text.splitlines():
        if _YAML_DOCUMENT_SEPARATOR.fullmatch(line):
            if has_document:
                document_index += 1
            else:
                has_document = True
            source_paths.setdefault(document_index, source_path)
            continue

        source_match = _HELM_SOURCE_LINE.fullmatch(line)
        if source_match is not None:
            source_path = _resolve_helm_source_path(root, chart, source_match.group(1))
            source_paths[document_index] = source_path
            has_document = True
        elif line.strip() and not line.lstrip().startswith("#"):
            has_document = True

    return source_paths


def _resolve_helm_source_path(root: Path, chart: _Chart, reference: str) -> str:
    value = reference.strip()
    if not value or value != reference or "\x00" in value or "\\" in value:
        return chart.source_path
    if value.startswith("/") or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive:
        return chart.source_path
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return chart.source_path

    candidates = [parts]
    if parts[0] == chart.name and len(parts) > 1:
        candidates.append(parts[1:])
    candidates.append(("charts", *parts))
    if parts[0] == chart.name and len(parts) > 1:
        candidates.append(("charts", *parts[1:]))

    chart_files = set(chart.files)
    for candidate_parts in candidates:
        chart_candidate = chart.directory.joinpath(*candidate_parts)
        if chart_candidate in chart_files:
            return chart_candidate.relative_to(root).as_posix()
        repository_candidate = root.joinpath(*candidate_parts)
        if repository_candidate in chart_files:
            return repository_candidate.relative_to(root).as_posix()
    return chart.source_path


def _decode_template(content: bytes, path: Path) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HelmConfigurationError(f"Helm template {path.name} must be valid UTF-8") from exc


def _unvalidated_functions(text: str) -> tuple[tuple[str, ...], bool]:
    found: set[str] = set()
    uses_tpl = False
    for match in _TEMPLATE_ACTION.finditer(text):
        action = _TEMPLATE_COMMENT.sub("", match.group(1))
        for function in _UNVALIDATED_TEMPLATE_FUNCTIONS:
            if re.search(rf"\b{re.escape(function)}\b", action):
                found.add(function)
        if re.search(r"\btpl\b", action):
            uses_tpl = True
    return tuple(sorted(found)), uses_tpl


def _validate_release_identity(release_name: str, namespace: str) -> None:
    if not isinstance(release_name, str) or len(release_name) > 53 or _DNS_LABEL.fullmatch(release_name) is None:
        raise HelmConfigurationError("release_name must be a DNS label of at most 53 characters")
    if not isinstance(namespace, str) or len(namespace) > 63 or _DNS_LABEL.fullmatch(namespace) is None:
        raise HelmConfigurationError("namespace must be a Kubernetes DNS label of at most 63 characters")


def _validate_kubernetes_version(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or _KUBERNETES_VERSION.fullmatch(value) is None:
        raise ValueError("kube_version must be a valid, trimmed Kubernetes version")
    return value.removeprefix("v")


def _validate_api_versions(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError("api_versions must be a sequence of Kubernetes API versions")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or _API_VERSION.fullmatch(value) is None:
            raise ValueError("api_versions must contain valid Kubernetes API version identifiers")
        normalized.add(value)
    return tuple(sorted(normalized))


def _configured_api_versions(values: Sequence[str] | None) -> tuple[str, ...]:
    if values is not None:
        return _validate_api_versions(values)
    raw = os.getenv("GITOPS_HELM_API_VERSIONS_JSON")
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("GITOPS_HELM_API_VERSIONS_JSON must be a JSON array of Kubernetes API versions") from exc
    if not isinstance(parsed, list):
        raise ValueError("GITOPS_HELM_API_VERSIONS_JSON must be a JSON array of Kubernetes API versions")
    return _validate_api_versions(parsed)
