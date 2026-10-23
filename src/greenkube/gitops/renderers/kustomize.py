"""Bounded rendering of local Kustomize overlays from repository snapshots."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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

_KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
_REMOTE_HOST = re.compile(r"^(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+(?::[0-9]+)?/")
_CHART_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_VERSION_MAX_BYTES = 4_096


@dataclass(frozen=True, slots=True)
class KustomizeLimits:
    """Resource limits applied before and during a Kustomize build."""

    timeout_seconds: float = 30.0
    max_output_bytes: int = 4_194_304
    max_stderr_bytes: int = 65_536
    max_kustomization_bytes: int = 1_048_576
    max_total_config_bytes: int = 4_194_304
    max_kustomization_files: int = 256
    max_kustomization_depth: int = 32

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
            ("max_kustomization_bytes", self.max_kustomization_bytes),
            ("max_total_config_bytes", self.max_total_config_bytes),
            ("max_kustomization_files", self.max_kustomization_files),
            ("max_kustomization_depth", self.max_kustomization_depth),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class KustomizeVersionMetadata:
    """Observed version and required pin for the Kustomize executable."""

    binary_name: str
    version: str
    expected_version: str

    @property
    def is_pinned(self) -> bool:
        return self.version == self.expected_version


class KustomizeError(RuntimeError):
    """Base error for invalid Kustomize input or rendering failures."""


class KustomizePathError(KustomizeError):
    """An overlay, base, or chart path is invalid or escapes the snapshot."""


class KustomizeConfigurationError(KustomizeError):
    """A Kustomization file is malformed or exceeds its configured limits."""


class KustomizeHelmUnsupportedError(KustomizeConfigurationError):
    """Kustomize Helm execution is disabled until equivalent Helm checks are available."""


class KustomizeLimitError(KustomizeError):
    """The Kustomize input graph exceeded a configured limit."""


class KustomizeRemoteReferenceError(KustomizeConfigurationError):
    """A remote base or chart was rejected because remote access is disabled."""


class KustomizeVersionError(KustomizeError):
    """The Kustomize executable did not report a usable version."""


class KustomizeVersionMismatchError(KustomizeVersionError):
    """The installed Kustomize version does not match the required pin."""


class KustomizeUnavailableError(KustomizeError):
    """The pinned Kustomize executable is not installed or is not executable."""


class KustomizeExecutionError(KustomizeError):
    """Kustomize could not be started or failed while building the overlay."""


class KustomizeIsolationUnavailableError(KustomizeExecutionError):
    """Kustomize execution was refused because an isolated renderer is not configured."""


class KustomizeTimeoutError(KustomizeError):
    """The Kustomize process exceeded its wall-clock timeout."""


class KustomizeOutputLimitError(KustomizeLimitError):
    """Kustomize exceeded a configured stdout or stderr byte limit."""

    def __init__(self, stream: str, limit_bytes: int) -> None:
        self.stream = stream
        self.limit_bytes = limit_bytes
        super().__init__(f"Kustomize {stream} output limit of {limit_bytes} bytes exceeded")


class KustomizeBuildError(KustomizeExecutionError):
    """Kustomize returned a non-zero exit status."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        super().__init__(f"Kustomize build failed with exit status {returncode}")


class KustomizeOutputError(KustomizeError):
    """Kustomize output could not be normalized as Kubernetes resources."""


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


@dataclass(slots=True)
class _ScanState:
    visited: set[Path] = field(default_factory=set)
    active: set[Path] = field(default_factory=set)
    config_files: int = 0
    total_config_bytes: int = 0
    has_helm_charts: bool = False


@dataclass(frozen=True, slots=True)
class _Overlay:
    directory: Path
    kustomization_file: Path
    has_helm_charts: bool


class KustomizeRenderer:
    """Build a confined snapshot overlay and normalize its output as a raw result."""

    def __init__(
        self,
        *,
        expected_version: str,
        binary: str = "kustomize",
        limits: KustomizeLimits | None = None,
        raw_limits: RenderLimits | None = None,
        runner: _Runner | None = None,
    ) -> None:
        if (
            not isinstance(expected_version, str)
            or not expected_version
            or expected_version != expected_version.strip()
        ):
            raise ValueError("expected_version must be a non-empty, trimmed Kustomize version")
        if not isinstance(binary, str) or not binary or "\x00" in binary:
            raise ValueError("binary must be a non-empty executable name or path")
        self.expected_version = expected_version
        self.binary = binary
        self.limits = limits or KustomizeLimits()
        self.raw_limits = raw_limits or RenderLimits()
        self._runner = runner or SandboxRunner()
        self._version_metadata: KustomizeVersionMetadata | None = None

    @property
    def version_metadata(self) -> KustomizeVersionMetadata | None:
        """Return the latest successful version preflight metadata, if available."""
        return self._version_metadata

    def _ensure_execution_backend(self) -> None:
        if isinstance(self._runner, SandboxRunner):
            try:
                self._runner.ensure_isolated()
            except SandboxIsolationUnavailableError as exc:
                raise KustomizeIsolationUnavailableError(
                    "Kustomize rendering is disabled because no isolated renderer backend is configured"
                ) from exc

    def preflight(self, snapshot: RepositorySnapshot) -> KustomizeVersionMetadata:
        """Verify that the executable in the snapshot workspace matches its required version pin."""
        self._ensure_execution_backend()
        workspace = _snapshot_workspace(snapshot)
        result = self._run((self.binary, "version"), workspace)
        if result.returncode != 0:
            raise KustomizeVersionError("Kustomize version command failed")

        if not result.stdout or len(result.stdout) > _VERSION_MAX_BYTES:
            raise KustomizeVersionError("Kustomize version output is empty or invalid")
        try:
            lines = result.stdout.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise KustomizeVersionError("Kustomize version output is not valid UTF-8") from exc
        version = next((line.strip() for line in lines if line.strip()), "")
        if not version or "\x00" in version:
            raise KustomizeVersionError("Kustomize version output is empty or invalid")

        metadata = KustomizeVersionMetadata(
            binary_name=Path(self.binary).name,
            version=version,
            expected_version=self.expected_version,
        )
        self._version_metadata = metadata
        if not metadata.is_pinned:
            raise KustomizeVersionMismatchError(
                f"Kustomize version {metadata.version!r} does not match expected pin {metadata.expected_version!r}"
            )
        return metadata

    def render(self, snapshot: RepositorySnapshot, overlay_path: str | Path) -> RawRenderResult:
        """Render an overlay that is wholly contained in the supplied repository snapshot."""
        self._ensure_execution_backend()
        workspace = _snapshot_workspace(snapshot)
        overlay = _inspect_overlay(workspace, overlay_path, self.limits)
        if overlay.has_helm_charts:
            raise KustomizeHelmUnsupportedError(
                "Kustomize helmCharts are disabled until an isolated renderer can enforce Helm "
                "determinism, schema, dependency, and version checks"
            )
        self.preflight(snapshot)

        arguments = [
            self.binary,
            "build",
            overlay.directory.relative_to(workspace).as_posix() or ".",
            "--load-restrictor=LoadRestrictionsRootOnly",
        ]
        process_result = self._run(arguments, workspace)
        if process_result.returncode != 0:
            raise KustomizeBuildError(process_result.returncode)
        if len(process_result.stdout) > self.limits.max_output_bytes:
            raise KustomizeOutputLimitError("stdout", self.limits.max_output_bytes)
        if len(process_result.stderr) > self.limits.max_stderr_bytes:
            raise KustomizeOutputLimitError("stderr", self.limits.max_stderr_bytes)

        source_path = overlay.kustomization_file.relative_to(workspace).as_posix()
        try:
            raw_result = render_raw({source_path: process_result.stdout}, limits=self.raw_limits)
        except RawRenderError as exc:
            raise KustomizeOutputError("Kustomize output is not a valid bounded Kubernetes manifest set") from exc
        return replace(raw_result, renderer=RendererKind.KUSTOMIZE)

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
        except SandboxCommandNotFoundError as exc:
            raise KustomizeUnavailableError("the pinned Kustomize executable is unavailable") from exc
        except FileNotFoundError as exc:
            raise KustomizeUnavailableError("the pinned Kustomize executable is unavailable") from exc
        except SandboxTimeoutError as exc:
            raise KustomizeTimeoutError("Kustomize render timed out at its configured limit") from exc
        except TimeoutError as exc:
            raise KustomizeTimeoutError("Kustomize render timed out at its configured limit") from exc
        except SandboxOutputLimitError as exc:
            raise KustomizeOutputLimitError(exc.stream, exc.limit_bytes) from exc
        except SandboxPathError as exc:
            raise KustomizePathError("Kustomize process paths must remain inside the snapshot workspace") from exc
        except SandboxIsolationUnavailableError as exc:
            raise KustomizeIsolationUnavailableError(
                "Kustomize rendering is disabled because no isolated renderer backend is configured"
            ) from exc
        except SandboxError as exc:
            raise KustomizeExecutionError("Kustomize sandbox execution failed") from exc
        if not isinstance(result, SandboxResult):
            raise KustomizeExecutionError("Kustomize runner returned an invalid process result")
        if len(result.stdout) > self.limits.max_output_bytes:
            raise KustomizeOutputLimitError("stdout", self.limits.max_output_bytes)
        if len(result.stderr) > self.limits.max_stderr_bytes:
            raise KustomizeOutputLimitError("stderr", self.limits.max_stderr_bytes)
        return result


def _snapshot_workspace(snapshot: RepositorySnapshot) -> Path:
    try:
        workspace = Path(snapshot.workspace)
        if workspace.is_symlink():
            raise KustomizePathError("snapshot workspace must not be a symbolic link")
        root = workspace.resolve(strict=True)
    except KustomizePathError:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise KustomizePathError("snapshot workspace must be an existing directory") from exc
    if not root.is_dir():
        raise KustomizePathError("snapshot workspace must be an existing directory")
    return root


def _inspect_overlay(root: Path, overlay_path: str | Path, limits: KustomizeLimits) -> _Overlay:
    if not isinstance(overlay_path, (str, Path)):
        raise KustomizePathError("overlay path must be a relative snapshot path")
    reference = str(overlay_path)
    if not reference or reference != reference.strip() or "\\" in reference or "\x00" in reference:
        raise KustomizePathError("overlay path must be a non-empty relative POSIX path")
    path_parts = PurePosixPath(reference).parts
    if path_parts and path_parts[0].startswith("-"):
        raise KustomizePathError("overlay path must not begin with an option marker")
    if reference != "." and (
        reference.startswith("/")
        or PurePosixPath(reference).is_absolute()
        or PureWindowsPath(reference).drive
        or ".." in PurePosixPath(reference).parts
    ):
        raise KustomizePathError("overlay path must not be absolute or traverse outside the snapshot")
    overlay = root if reference == "." else _resolve_local_path(root, root, reference, required=True)
    if not overlay.is_dir():
        raise KustomizePathError("overlay path must identify a directory inside the snapshot")

    state = _ScanState()
    _scan_kustomization(root, overlay, limits, state, depth=0)
    kustomization_file = _find_kustomization_file(root, overlay, required=True)
    if kustomization_file is None:
        raise KustomizeConfigurationError("overlay directory does not contain a Kustomization file")
    return _Overlay(overlay, kustomization_file, state.has_helm_charts)


def _scan_kustomization(
    root: Path,
    directory: Path,
    limits: KustomizeLimits,
    state: _ScanState,
    *,
    depth: int,
) -> None:
    if depth > limits.max_kustomization_depth:
        raise KustomizeLimitError(f"Kustomize base depth limit of {limits.max_kustomization_depth} exceeded")
    if directory in state.active:
        raise KustomizeConfigurationError("recursive Kustomize base or component reference")
    if directory in state.visited:
        return

    kustomization_file = _find_kustomization_file(root, directory, required=True)
    if kustomization_file is None:
        raise KustomizeConfigurationError("local base or component has no Kustomization file")
    state.active.add(directory)
    try:
        config = _load_yaml_mapping(kustomization_file, state, limits, category="Kustomization")
        for field_name in ("resources", "bases", "components"):
            for reference in _string_list(config, field_name, kustomization_file):
                _reject_remote_reference(reference, "base")
                local_path = _resolve_local_path(root, directory, reference, required=True)
                if local_path.is_dir():
                    if _find_kustomization_file(root, local_path, required=False) is None:
                        raise KustomizeConfigurationError("local base or component directory has no Kustomization file")
                    _scan_kustomization(root, local_path, limits, state, depth=depth + 1)
                elif not local_path.is_file():
                    raise KustomizePathError("local Kustomize resource must be a file or Kustomization directory")

        _scan_helm_charts(root, directory, config, state, limits, kustomization_file)
    finally:
        state.active.remove(directory)
    state.visited.add(directory)


def _scan_helm_charts(
    root: Path,
    directory: Path,
    config: Mapping[str, Any],
    state: _ScanState,
    limits: KustomizeLimits,
    kustomization_file: Path,
) -> None:
    helm_globals = config.get("helmGlobals", {})
    if helm_globals is None:
        helm_globals = {}
    if not isinstance(helm_globals, Mapping):
        raise KustomizeConfigurationError(f"{kustomization_file.name}: helmGlobals must be a mapping")

    helm_charts = config.get("helmCharts", [])
    if helm_charts is None:
        helm_charts = []
    if not isinstance(helm_charts, list) or any(not isinstance(chart, Mapping) for chart in helm_charts):
        raise KustomizeConfigurationError(f"{kustomization_file.name}: helmCharts must be a list of mappings")
    for chart in helm_charts:
        repository = chart.get("repo")
        if repository not in (None, ""):
            if not isinstance(repository, str):
                raise KustomizeConfigurationError(f"{kustomization_file.name}: helmCharts.repo must be a string")
            raise KustomizeRemoteReferenceError("remote Helm charts are disabled")

    chart_home_value = helm_globals.get("chartHome", "charts")
    if not isinstance(chart_home_value, str) or not chart_home_value:
        raise KustomizeConfigurationError(f"{kustomization_file.name}: helmGlobals.chartHome must be a path")
    if "chartHome" in helm_globals or helm_charts:
        chart_home = _resolve_local_path(root, directory, chart_home_value, required=True)
        if not chart_home.is_dir():
            raise KustomizePathError("Kustomize chartHome must be a directory inside the snapshot")
    else:
        chart_home = directory / "charts"

    config_home = helm_globals.get("configHome")
    if config_home is not None:
        if not isinstance(config_home, str) or not config_home:
            raise KustomizeConfigurationError(f"{kustomization_file.name}: helmGlobals.configHome must be a path")
        _resolve_local_path(root, directory, config_home, required=False)

    if helm_charts:
        state.has_helm_charts = True

    for chart in helm_charts:
        name = chart.get("name")
        if not isinstance(name, str) or not _CHART_NAME.fullmatch(name) or name in {".", ".."}:
            raise KustomizeConfigurationError(
                f"{kustomization_file.name}: helmCharts entries require a local chart name"
            )
        chart_directory = _resolve_local_path(root, chart_home, name, required=True)
        if not chart_directory.is_dir():
            raise KustomizePathError("local Helm chart directory must be inside the snapshot")
        chart_metadata = _resolve_local_path(root, chart_directory, "Chart.yaml", required=True)
        if not chart_metadata.is_file():
            raise KustomizePathError("local Helm chart must contain Chart.yaml")
        chart_config = _load_yaml_mapping(chart_metadata, state, limits, category="Helm Chart")
        _validate_chart_dependencies(root, chart_directory, chart_config, kustomization_file)

        for values_key in ("valuesFile",):
            values_file = chart.get(values_key)
            if values_file is not None:
                if not isinstance(values_file, str) or not values_file:
                    raise KustomizeConfigurationError(
                        f"{kustomization_file.name}: helmCharts.{values_key} must be a path"
                    )
                _resolve_local_path(root, directory, values_file, required=False)
                _resolve_local_path(root, chart_directory, values_file, required=True)
        additional_values_files = chart.get("additionalValuesFiles", [])
        if additional_values_files is None:
            additional_values_files = []
        if not isinstance(additional_values_files, list) or any(
            not isinstance(values_file, str) or not values_file for values_file in additional_values_files
        ):
            raise KustomizeConfigurationError(
                f"{kustomization_file.name}: helmCharts.additionalValuesFiles must be a list of paths"
            )
        for values_file in additional_values_files:
            _resolve_local_path(root, directory, values_file, required=False)
            _resolve_local_path(root, chart_directory, values_file, required=True)


def _validate_chart_dependencies(
    root: Path,
    chart_directory: Path,
    chart_config: Mapping[str, Any],
    kustomization_file: Path,
) -> None:
    dependencies = chart_config.get("dependencies", [])
    if dependencies is None:
        return
    if not isinstance(dependencies, list):
        raise KustomizeConfigurationError(f"{kustomization_file.name}: local chart dependencies must be a list")
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            raise KustomizeConfigurationError(f"{kustomization_file.name}: local chart dependency must be a mapping")
        repository = dependency.get("repository")
        if repository in (None, ""):
            continue
        if not isinstance(repository, str):
            raise KustomizeConfigurationError(
                f"{kustomization_file.name}: chart dependency repository must be a string"
            )
        if repository.startswith("file://"):
            local_reference = repository.removeprefix("file://")
            local_path = _resolve_local_path(root, chart_directory, local_reference, required=True)
            if not local_path.is_dir():
                raise KustomizePathError("local chart dependency must be a directory inside the snapshot")
        else:
            raise KustomizeRemoteReferenceError("remote Helm chart dependencies are disabled")


def _find_kustomization_file(root: Path, directory: Path, *, required: bool) -> Path | None:
    if not directory.is_relative_to(root):
        raise KustomizePathError("Kustomization directory must remain inside the snapshot workspace")
    found: list[Path] = []
    for name in _KUSTOMIZATION_NAMES:
        candidate = directory / name
        if candidate.is_symlink():
            raise KustomizePathError("Kustomization files must not be symbolic links")
        if candidate.exists():
            if not candidate.is_file():
                raise KustomizePathError("Kustomization path must identify a regular file")
            found.append(candidate)
    if len(found) > 1:
        raise KustomizeConfigurationError("overlay contains multiple Kustomization file names")
    if not found and required:
        return None
    return found[0] if found else None


def _load_yaml_mapping(
    path: Path,
    state: _ScanState,
    limits: KustomizeLimits,
    *,
    category: str,
) -> Mapping[str, Any]:
    if state.config_files >= limits.max_kustomization_files:
        raise KustomizeLimitError(f"Kustomize config file limit of {limits.max_kustomization_files} exceeded")
    try:
        with path.open("rb") as source:
            content = source.read(limits.max_kustomization_bytes + 1)
    except OSError as exc:
        raise KustomizePathError(f"{category} file could not be read inside the snapshot") from exc
    if len(content) > limits.max_kustomization_bytes:
        raise KustomizeLimitError(f"{category} file byte limit of {limits.max_kustomization_bytes} exceeded")
    state.total_config_bytes += len(content)
    if state.total_config_bytes > limits.max_total_config_bytes:
        raise KustomizeLimitError(f"Kustomize total config byte limit of {limits.max_total_config_bytes} exceeded")
    state.config_files += 1

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KustomizeConfigurationError(f"{category} file must be valid UTF-8") from exc

    parser = YAML(typ="safe", pure=True)
    parser.version = (1, 2)
    parser.allow_duplicate_keys = False
    try:
        value = parser.load(text)
    except (YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise KustomizeConfigurationError(f"{category} file is invalid YAML ({type(exc).__name__})") from exc
    if not isinstance(value, Mapping):
        raise KustomizeConfigurationError(f"{category} file must contain a mapping")
    return value


def _string_list(config: Mapping[str, Any], field_name: str, source: Path) -> tuple[str, ...]:
    values = config.get(field_name, [])
    if values is None:
        return ()
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise KustomizeConfigurationError(f"{source.name}: {field_name} must be a list of local paths")
    return tuple(values)


def _reject_remote_reference(reference: str, kind: str) -> None:
    value = reference.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise KustomizeRemoteReferenceError(f"remote Kustomize {kind} references are disabled") from exc
    remote = (
        bool(parsed.scheme)
        or value.startswith("//")
        or value.startswith("git@")
        or _REMOTE_HOST.match(value) is not None
    )
    if remote:
        raise KustomizeRemoteReferenceError(f"remote Kustomize {kind} references are disabled")


def _resolve_local_path(root: Path, base: Path, reference: str, *, required: bool) -> Path:
    if (
        not isinstance(reference, str)
        or not reference
        or reference != reference.strip()
        or "\\" in reference
        or "\x00" in reference
        or reference.startswith("/")
        or PurePosixPath(reference).is_absolute()
        or PureWindowsPath(reference).drive
    ):
        raise KustomizePathError("local Kustomize paths must be relative POSIX paths")
    if not base.is_relative_to(root):
        raise KustomizePathError("Kustomize path base must remain inside the snapshot workspace")

    parts = PurePosixPath(reference).parts
    if not parts or any(part == "" for part in reference.split("/")):
        raise KustomizePathError("local Kustomize path must not contain empty components")

    current = base
    for part in parts:
        if part == ".":
            continue
        if part == "..":
            if not current.is_dir():
                raise KustomizePathError("Kustomize path traverses a missing or non-directory component")
            current = current.parent
            if not current.is_relative_to(root):
                raise KustomizePathError("Kustomize path escapes the snapshot workspace")
            continue
        current = current / part
        if current.is_symlink():
            raise KustomizePathError("Kustomize paths must not traverse symbolic links")

    try:
        resolved = current.resolve(strict=required)
    except (OSError, RuntimeError) as exc:
        raise KustomizePathError("local Kustomize path does not exist inside the snapshot") from exc
    if not resolved.is_relative_to(root):
        raise KustomizePathError("Kustomize path escapes the snapshot workspace")
    if required and not resolved.exists():
        raise KustomizePathError("local Kustomize path does not exist inside the snapshot")
    return resolved
