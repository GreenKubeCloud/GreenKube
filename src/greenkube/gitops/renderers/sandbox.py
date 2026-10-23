"""Fail-closed isolated execution for repository-controlled renderers."""

from __future__ import annotations

import base64
import json
import math
import os
import selectors
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

_READ_CHUNK_BYTES = 64 * 1024
_TERMINATION_GRACE_SECONDS = 2.0
_SAFE_ENVIRONMENT_KEYS = ("LANG", "LC_ALL", "LC_CTYPE", "PATH", "SYSTEMROOT", "TZ")
_SANDBOX_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
_SANDBOX_PROCESS_LIMIT = 64
_SANDBOX_WORKSPACE_ROOT = "/greenkube-render-workspace"
_REMOTE_WORKSPACE_MARKER = "__GREENCUBE_WORKSPACE__"
_REMOTE_MAX_FILES = 20_000
_REMOTE_MAX_FILE_BYTES = 2 * 1024 * 1024
_REMOTE_MAX_EXTRACTED_BYTES = 500 * 1024 * 1024
_REMOTE_MAX_COMPRESSED_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Captured process output after successful bounded execution."""

    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float = 0.0


class SandboxError(RuntimeError):
    """Base error for a rejected or failed sandbox process."""


class SandboxPathError(SandboxError):
    """The workspace or working directory is invalid or outside the workspace."""


class SandboxCommandNotFoundError(SandboxError):
    """The requested executable is not available."""


class SandboxExecutionError(SandboxError):
    """The process could not be started or safely completed."""


class SandboxIsolationUnavailableError(SandboxError):
    """No isolated renderer backend is configured, so repository code must not run."""


class SandboxTimeoutError(SandboxError):
    """The process exceeded its wall-clock timeout."""


class SandboxOutputLimitError(SandboxError):
    """The process exceeded a configured stdout or stderr byte limit."""

    def __init__(self, stream: str, limit_bytes: int) -> None:
        self.stream = stream
        self.limit_bytes = limit_bytes
        super().__init__(f"sandbox {stream} output limit of {limit_bytes} bytes exceeded")


class SandboxRunner:
    """Run pinned renderers in Linux namespaces with a read-only snapshot."""

    def __init__(
        self,
        *,
        bubblewrap_binary: str = "bwrap",
        prlimit_binary: str = "prlimit",
        process_runner: BoundedSubprocessRunner | None = None,
        renderer_url: str | None = None,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not isinstance(bubblewrap_binary, str) or not bubblewrap_binary.strip():
            raise ValueError("bubblewrap_binary must be a non-empty executable name")
        if not isinstance(prlimit_binary, str) or not prlimit_binary.strip():
            raise ValueError("prlimit_binary must be a non-empty executable name")
        self.bubblewrap_binary = bubblewrap_binary
        self.prlimit_binary = prlimit_binary
        self._process_runner = process_runner or BoundedSubprocessRunner()
        self._isolation_tools: tuple[str, str] | None = None
        self._http_transport = http_transport
        raw_renderer_url = renderer_url if renderer_url is not None else os.getenv("GITOPS_RENDERER_URL")
        self._renderer_url = _validate_renderer_url(raw_renderer_url) if raw_renderer_url else None
        if self._renderer_url is not None:
            self._renderer_url = _validate_renderer_url(self._renderer_url)

    def _tools(self) -> tuple[str, str]:
        if self._isolation_tools is not None:
            return self._isolation_tools
        if os.name != "posix":
            raise SandboxIsolationUnavailableError("isolated rendering requires Linux namespace support")
        bubblewrap = shutil.which(self.bubblewrap_binary)
        prlimit = shutil.which(self.prlimit_binary)
        if bubblewrap is None or prlimit is None:
            raise SandboxIsolationUnavailableError(
                "no isolated renderer backend is available because bubblewrap and prlimit are missing"
            )
        self._isolation_tools = (bubblewrap, prlimit)
        return self._isolation_tools

    def ensure_isolated(self) -> None:
        if self._renderer_url is not None:
            return
        bubblewrap, prlimit = self._tools()
        with tempfile.TemporaryDirectory(prefix="greenkube-render-probe-") as temporary_workspace:
            workspace = Path(temporary_workspace).resolve()
            argv = _sandbox_command(
                ("/usr/bin/true",),
                workspace=workspace,
                cwd=workspace,
                bubblewrap_binary=bubblewrap,
                prlimit_binary=prlimit,
                timeout_seconds=2,
                probe=True,
            )
            try:
                result = self._process_runner.run(
                    argv,
                    workspace=workspace,
                    cwd=workspace,
                    timeout_seconds=2,
                    max_stdout_bytes=1_024,
                    max_stderr_bytes=1_024,
                )
            except SandboxError as exc:
                raise SandboxIsolationUnavailableError(
                    "no isolated renderer backend is available because the runtime cannot establish Linux namespaces"
                ) from exc
        if result.returncode != 0:
            raise SandboxIsolationUnavailableError(
                "no isolated renderer backend is available because the runtime cannot establish Linux namespaces"
            )

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: str | Path,
        cwd: str | Path | None = None,
        timeout_seconds: float = 30.0,
        max_stdout_bytes: int = 4_194_304,
        max_stderr_bytes: int = 65_536,
    ) -> SandboxResult:
        arguments = _validated_arguments(argv)
        workspace_path = _resolve_workspace(workspace)
        working_directory = _resolve_working_directory(workspace_path, cwd)
        self.ensure_isolated()
        if self._renderer_url is not None:
            return self._run_remote(
                arguments,
                workspace=workspace_path,
                cwd=working_directory,
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
            )
        bubblewrap, prlimit = self._tools()
        sandboxed_argv = _sandbox_command(
            arguments,
            workspace=workspace_path,
            cwd=working_directory,
            bubblewrap_binary=bubblewrap,
            prlimit_binary=prlimit,
            timeout_seconds=timeout_seconds,
        )
        return self._process_runner.run(
            sandboxed_argv,
            workspace=workspace_path,
            cwd=working_directory,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )

    def _run_remote(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        timeout_seconds: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> SandboxResult:
        _validate_renderer_executable(argv[0])
        relative_cwd = cwd.relative_to(workspace).as_posix()
        metadata = {
            "argv": [
                _remote_argument(argument, workspace, is_executable=index == 0) for index, argument in enumerate(argv)
            ],
            "cwd": relative_cwd,
            "timeout_seconds": timeout_seconds,
            "max_stdout_bytes": max_stdout_bytes,
            "max_stderr_bytes": max_stderr_bytes,
        }
        header = base64.urlsafe_b64encode(json.dumps(metadata, separators=(",", ":")).encode("utf-8")).decode("ascii")
        if len(header) > 16_384:
            raise SandboxExecutionError("renderer command metadata exceeds its supported size")
        try:
            with _workspace_archive(workspace) as archive:
                with httpx.Client(
                    timeout=httpx.Timeout(timeout_seconds + 10, connect=5),
                    trust_env=False,
                    transport=self._http_transport,
                ) as client:
                    response = client.post(
                        f"{self._renderer_url}/render",
                        content=_archive_chunks(archive),
                        headers={
                            "Content-Type": "application/vnd.greenkube.snapshot+tar",
                            "X-GK-Render-Request": header,
                        },
                    )
        except httpx.HTTPError as exc:
            raise SandboxExecutionError("isolated renderer service is unavailable") from exc
        if response.status_code != 200:
            raise SandboxExecutionError(f"isolated renderer service rejected the request (HTTP {response.status_code})")
        try:
            payload = response.json()
            returncode = payload["returncode"]
            stdout = base64.b64decode(payload["stdout"], validate=True)
            stderr = base64.b64decode(payload["stderr"], validate=True)
            duration = payload["duration_seconds"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SandboxExecutionError("isolated renderer service returned an invalid response") from exc
        if (
            type(returncode) is not int
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or len(stdout) > max_stdout_bytes
            or len(stderr) > max_stderr_bytes
        ):
            raise SandboxExecutionError("isolated renderer service response exceeded its declared limits")
        return SandboxResult(returncode=returncode, stdout=stdout, stderr=stderr, duration_seconds=float(duration))


def _sandbox_command(
    argv: Sequence[str],
    *,
    workspace: Path,
    cwd: Path,
    bubblewrap_binary: str,
    prlimit_binary: str,
    timeout_seconds: float,
    probe: bool = False,
) -> tuple[str, ...]:
    timeout = _positive_timeout(timeout_seconds)
    workspace = _resolve_workspace(workspace)
    working_directory = _resolve_working_directory(workspace, cwd)
    arguments = _validated_arguments(argv)
    if not probe:
        _validate_renderer_executable(arguments[0])
    mapped_arguments = tuple(
        _map_workspace_argument(argument, workspace, is_executable=index == 0)
        for index, argument in enumerate(arguments)
    )
    relative_cwd = working_directory.relative_to(workspace).as_posix()
    sandbox_cwd = _SANDBOX_WORKSPACE_ROOT if relative_cwd == "." else f"{_SANDBOX_WORKSPACE_ROOT}/{relative_cwd}"
    masks = ["/var/run/secrets", "/run", "/etc/greenkube"]
    for variable in ("GITOPS_SECRET_MOUNT", "GREENKUBE_WEBHOOK_SECRET_MOUNT"):
        mount_path = os.environ.get(variable)
        if mount_path and Path(mount_path).is_absolute() and mount_path != "/" and Path(mount_path).exists():
            if not any(mount_path == root or mount_path.startswith(f"{root}/") for root in masks):
                masks.append(mount_path)

    command: list[str] = [
        bubblewrap_binary,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/",
        "/",
        "--ro-bind",
        str(workspace),
        _SANDBOX_WORKSPACE_ROOT,
        "--tmpfs",
        "/tmp",
    ]
    for mask in masks:
        command.extend(("--tmpfs", mask))
    command.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "LANG",
            "C",
            "--setenv",
            "HELM_CACHE_HOME",
            "/tmp/helm/cache",
            "--setenv",
            "HELM_CONFIG_HOME",
            "/tmp/helm/config",
            "--setenv",
            "HELM_DATA_HOME",
            "/tmp/helm/data",
            "--chdir",
            sandbox_cwd,
            "--",
            prlimit_binary,
            f"--as={_SANDBOX_MEMORY_LIMIT_BYTES}",
            f"--cpu={max(1, math.ceil(timeout))}",
            f"--nproc={_SANDBOX_PROCESS_LIMIT}",
            "--nofile=256",
            "--fsize=67108864",
            "--",
            *mapped_arguments,
        )
    )
    return tuple(command)


def _validate_renderer_executable(executable: str) -> None:
    name = Path(executable).name
    if name not in {"helm", "kustomize"}:
        raise SandboxExecutionError("only the pinned Helm and Kustomize executables may run in the renderer sandbox")
    if Path(executable).is_absolute():
        resolved = Path(executable).resolve()
        if not any(resolved.is_relative_to(prefix) for prefix in (Path("/usr/local/bin"), Path("/usr/bin"))):
            raise SandboxExecutionError("renderer executable must be installed in a trusted system directory")
    elif "/" in executable or executable.startswith("-"):
        raise SandboxExecutionError("renderer executable must be a trusted command name")


def _map_workspace_argument(argument: str, workspace: Path, *, is_executable: bool) -> str:
    if is_executable:
        return argument
    workspace_text = str(workspace)
    if argument == workspace_text:
        return _SANDBOX_WORKSPACE_ROOT
    if argument.startswith(f"{workspace_text}{os.sep}"):
        relative = Path(argument).relative_to(workspace).as_posix()
        return f"{_SANDBOX_WORKSPACE_ROOT}/{relative}"
    option_prefix, separator, option_value = argument.partition("=")
    if separator and option_value.startswith(f"{workspace_text}{os.sep}"):
        relative = Path(option_value).relative_to(workspace).as_posix()
        return f"{option_prefix}={_SANDBOX_WORKSPACE_ROOT}/{relative}"
    if argument.startswith("/"):
        raise SandboxPathError("renderer paths must remain inside the snapshot workspace")
    return argument


def _validate_renderer_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("GITOPS_RENDERER_URL must be a non-empty trimmed URL")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        raise ValueError("GITOPS_RENDERER_URL is invalid") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("GITOPS_RENDERER_URL must be an http(s) service URL without credentials or query")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _remote_argument(argument: str, workspace: Path, *, is_executable: bool) -> str:
    if is_executable:
        _validate_renderer_executable(argument)
        return Path(argument).name
    workspace_text = str(workspace)
    if argument == workspace_text:
        return _REMOTE_WORKSPACE_MARKER
    if argument.startswith(f"{workspace_text}{os.sep}"):
        relative = Path(argument).relative_to(workspace).as_posix()
        return f"{_REMOTE_WORKSPACE_MARKER}/{relative}"
    option, separator, value = argument.partition("=")
    if separator and value.startswith(f"{workspace_text}{os.sep}"):
        relative = Path(value).relative_to(workspace).as_posix()
        return f"{option}={_REMOTE_WORKSPACE_MARKER}/{relative}"
    if argument.startswith("/"):
        raise SandboxPathError("renderer paths must remain inside the snapshot workspace")
    return argument


@contextmanager
def _workspace_archive(workspace: Path) -> Iterator[BinaryIO]:
    root = _resolve_workspace(workspace)
    archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    total_bytes = 0
    file_count = 0
    try:
        with tarfile.open(fileobj=archive, mode="w:gz", format=tarfile.PAX_FORMAT) as output:
            for path in sorted(root.rglob("*")):
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise SandboxPathError("renderer snapshot file could not be inspected") from exc
                if stat.S_ISDIR(metadata.st_mode):
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise SandboxPathError("renderer snapshot cannot contain symbolic links or special files")
                relative = path.relative_to(root).as_posix()
                relative_path = Path(relative)
                if (
                    not relative
                    or relative.startswith("/")
                    or "\\" in relative
                    or "\x00" in relative
                    or any(part in {"", ".", ".."} for part in relative_path.parts)
                ):
                    raise SandboxPathError("renderer snapshot contains an invalid repository path")
                if metadata.st_size > _REMOTE_MAX_FILE_BYTES:
                    raise SandboxOutputLimitError("snapshot-file", _REMOTE_MAX_FILE_BYTES)
                total_bytes += metadata.st_size
                file_count += 1
                if total_bytes > _REMOTE_MAX_EXTRACTED_BYTES:
                    raise SandboxOutputLimitError("snapshot", _REMOTE_MAX_EXTRACTED_BYTES)
                if file_count > _REMOTE_MAX_FILES:
                    raise SandboxOutputLimitError("snapshot-files", _REMOTE_MAX_FILES)
                entry = tarfile.TarInfo(relative)
                entry.size = metadata.st_size
                entry.mode = 0o444
                entry.uid = 0
                entry.gid = 0
                entry.mtime = 0
                with path.open("rb") as source:
                    output.addfile(entry, source)
        compressed_bytes = archive.tell()
        if compressed_bytes > _REMOTE_MAX_COMPRESSED_BYTES:
            raise SandboxOutputLimitError("snapshot-archive", _REMOTE_MAX_COMPRESSED_BYTES)
        archive.seek(0)
        yield cast(BinaryIO, archive)
    except SandboxError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise SandboxExecutionError("renderer snapshot could not be archived safely") from exc
    finally:
        archive.close()


def _archive_chunks(archive: BinaryIO) -> Iterator[bytes]:
    while True:
        chunk = archive.read(_READ_CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


class BoundedSubprocessRunner:
    """Run a trusted local command with time/output bounds; this is not a security boundary."""

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: str | Path,
        cwd: str | Path | None = None,
        timeout_seconds: float = 30.0,
        max_stdout_bytes: int = 4_194_304,
        max_stderr_bytes: int = 65_536,
    ) -> SandboxResult:
        arguments = _validated_arguments(argv)
        timeout = _positive_timeout(timeout_seconds)
        stdout_limit = _positive_limit("max_stdout_bytes", max_stdout_bytes)
        stderr_limit = _positive_limit("max_stderr_bytes", max_stderr_bytes)
        workspace_path = _resolve_workspace(workspace)
        working_directory = _resolve_working_directory(workspace_path, cwd)
        started_at = time.monotonic()

        with tempfile.TemporaryDirectory(prefix=".greenkube-render-", dir=workspace_path) as environment_home:
            environment = _scrubbed_environment(Path(environment_home))
            try:
                process = subprocess.Popen(
                    arguments,
                    cwd=working_directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    close_fds=True,
                    start_new_session=os.name == "posix",
                )
            except FileNotFoundError as exc:
                raise SandboxCommandNotFoundError("sandbox executable is unavailable") from exc
            except OSError as exc:
                raise SandboxExecutionError("sandbox process could not be started") from exc

            try:
                stdout, stderr = _capture_bounded(
                    process,
                    timeout_seconds=timeout,
                    max_stdout_bytes=stdout_limit,
                    max_stderr_bytes=stderr_limit,
                )
                returncode = process.wait()
            except BaseException:
                _terminate_process(process)
                try:
                    process.wait(timeout=_TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise

        return SandboxResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started_at,
        )


def _validated_arguments(argv: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)) or not argv:
        raise SandboxExecutionError("sandbox command must be a non-empty argument sequence")
    arguments = tuple(argv)
    if any(not isinstance(argument, str) or not argument or "\x00" in argument for argument in arguments):
        raise SandboxExecutionError("sandbox arguments must be non-empty strings without NUL bytes")
    return arguments


def _positive_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    return float(value)


def _positive_limit(name: str, value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _resolve_workspace(workspace: str | Path) -> Path:
    try:
        workspace_path = Path(workspace)
        if workspace_path.is_symlink():
            raise SandboxPathError("snapshot workspace must not be a symbolic link")
        resolved = workspace_path.resolve(strict=True)
    except SandboxPathError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SandboxPathError("snapshot workspace must be an existing directory") from exc
    if not resolved.is_dir():
        raise SandboxPathError("snapshot workspace must be an existing directory")
    return resolved


def _resolve_working_directory(workspace: Path, cwd: str | Path | None) -> Path:
    candidate = workspace if cwd is None else Path(cwd)
    if not candidate.is_absolute():
        candidate = workspace / candidate

    try:
        relative = candidate.relative_to(workspace)
    except ValueError as exc:
        raise SandboxPathError("working directory must be inside the snapshot workspace") from exc

    current = workspace
    for part in relative.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            if not current.is_relative_to(workspace):
                raise SandboxPathError("working directory must be inside the snapshot workspace")
            continue
        current = current / part
        if current.is_symlink():
            raise SandboxPathError("working directory must not traverse symbolic links")

    try:
        resolved = current.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SandboxPathError("working directory must be an existing directory") from exc
    if not resolved.is_relative_to(workspace) or not resolved.is_dir():
        raise SandboxPathError("working directory must be an existing directory inside the snapshot workspace")
    return resolved


def _scrubbed_environment(home: Path) -> dict[str, str]:
    inherited = os.environ
    environment = {
        key: inherited[key] for key in _SAFE_ENVIRONMENT_KEYS if key in inherited and "\x00" not in inherited[key]
    }
    environment.setdefault("PATH", os.defpath)
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(home),
            "TMP": str(home),
            "TEMP": str(home),
            "USERPROFILE": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        }
    )
    return environment


def _capture_bounded(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise SandboxExecutionError("sandbox process output pipes are unavailable")

    stdout_pipe = cast(BinaryIO, process.stdout)
    stderr_pipe = cast(BinaryIO, process.stderr)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": max_stdout_bytes, "stderr": max_stderr_bytes}
    failure: tuple[str, str | None] | None = None
    deadline = time.monotonic() + timeout_seconds
    terminated_at: float | None = None

    with selectors.DefaultSelector() as selector:
        selector.register(stdout_pipe, selectors.EVENT_READ, "stdout")
        selector.register(stderr_pipe, selectors.EVENT_READ, "stderr")

        while process.poll() is None or selector.get_map():
            now = time.monotonic()
            if failure is None and now >= deadline:
                failure = ("timeout", None)
                terminated_at = now
                _terminate_process(process)
            if failure is not None and terminated_at is not None and now - terminated_at >= _TERMINATION_GRACE_SECONDS:
                _close_registered_pipes(selector)
                break

            if selector.get_map():
                wait_seconds = 0.05 if failure is not None else max(0.0, min(0.05, deadline - now))
                ready = selector.select(wait_seconds)
            else:
                time.sleep(0.01)
                ready = ()

            for key, _ in ready:
                stream_name = str(key.data)
                stream = cast(BinaryIO, key.fileobj)
                try:
                    chunk = os.read(stream.fileno(), _READ_CHUNK_BYTES)
                except OSError as exc:
                    raise SandboxExecutionError("sandbox process output could not be read") from exc
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue

                if failure is not None:
                    continue

                output = buffers[stream_name]
                available = limits[stream_name] - len(output)
                if len(chunk) > available:
                    if available > 0:
                        output.extend(chunk[:available])
                    failure = ("output", stream_name)
                    terminated_at = time.monotonic()
                    _terminate_process(process)
                else:
                    output.extend(chunk)

    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        process.wait()

    if failure is not None:
        failure_type, stream_name = failure
        if failure_type == "timeout":
            raise SandboxTimeoutError(f"sandbox process timed out after {timeout_seconds:g} seconds")
        if stream_name is not None:
            raise SandboxOutputLimitError(stream_name, limits[stream_name])
    return bytes(buffers["stdout"]), bytes(buffers["stderr"])


def _close_registered_pipes(selector: selectors.BaseSelector) -> None:
    for key in tuple(selector.get_map().values()):
        stream = cast(BinaryIO, key.fileobj)
        selector.unregister(stream)
        stream.close()


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()
