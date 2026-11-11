"""Isolated renderer service for bounded repository-controlled Helm/Kustomize commands."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from uvicorn import run

from greenkube.gitops.renderers.sandbox import BoundedSubprocessRunner, SandboxError
from greenkube.gitops.snapshots import SnapshotError, SnapshotLimitError, SnapshotLimits, extract_archive

_WORKSPACE_MARKER = "__GREENCUBE_WORKSPACE__"
_MAX_REQUEST_HEADER_BYTES = 16_384
_MAX_COMMAND_ARGUMENTS = 512
_MAX_ARGUMENT_BYTES = 8_192
_MAX_STDOUT_BYTES = 4 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_ADDRESS_SPACE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_PROCESS_COUNT = 64
_MAX_FILE_BYTES = 64 * 1024 * 1024
_ALLOWED_RENDERERS = frozenset({"helm", "kustomize"})
_RENDER_SLOTS = asyncio.Semaphore(1)

_SNAPSHOT_LIMITS = SnapshotLimits(
    max_compressed_bytes=100 * 1024 * 1024,
    max_extracted_bytes=500 * 1024 * 1024,
    max_files=20_000,
    max_file_bytes=2 * 1024 * 1024,
)


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1, max_length=_MAX_COMMAND_ARGUMENTS)
    cwd: str
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int


#: The egress canary runs at startup and then daily (F6).
EGRESS_PROBE_INTERVAL_SECONDS = 86_400.0
EGRESS_PROBE_TIMEOUT_SECONDS = 3.0
#: Lets the network plugin program the policy of a new pod before the first probe.
EGRESS_PROBE_INITIAL_DELAY_SECONDS = 15.0
_egress_state: dict[str, Any] = {"enforced": None, "checked_at": None, "target": None}


RESOLV_CONF = Path("/etc/resolv.conf")


def egress_probe_target(resolv_conf: Path = RESOLV_CONF) -> tuple[str, int] | None:
    """Where the canary connects.

    ``RENDERER_EGRESS_PROBE_TARGET`` (``host:port``) when set; otherwise the cluster DNS
    Service on TCP 53 (the first ``nameserver`` of ``/etc/resolv.conf``), served by
    ordinary pods so every NetworkPolicy implementation applies to it; otherwise the
    Kubernetes API Service address. Some CNIs (kindnet) always allow traffic to the API
    server, so it is only the last resort.
    """
    override = os.getenv("RENDERER_EGRESS_PROBE_TARGET", "").strip()
    if override:
        host, _, port = override.rpartition(":")
        return (host.strip("[]"), int(port)) if host and port.isdecimal() else None
    try:
        for line in resolv_conf.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                return (fields[1], 53)
    except OSError:
        pass
    host = os.getenv("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.getenv("KUBERNETES_SERVICE_PORT", "443").strip()
    return (host, int(port)) if host and port.isdecimal() else None


async def probe_egress(target: tuple[str, int] | None, *, timeout: float = EGRESS_PROBE_TIMEOUT_SECONDS) -> bool | None:
    """True when egress is denied (the connection fails), False when it is open, None if unknown.

    The renderer holds no Secrets and no service-account token, so an unenforced policy
    is a warning (``renderer_egress_unenforced``), not a blocker.
    """
    if target is None:
        return None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(*target), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return True
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return False


async def refresh_egress_state() -> dict[str, Any]:
    target = egress_probe_target()
    enforced = await probe_egress(target)
    _egress_state.update(
        enforced=enforced,
        checked_at=datetime.now(timezone.utc).isoformat(),
        target=f"{target[0]}:{target[1]}" if target else None,
    )
    return dict(_egress_state)


async def _egress_canary_loop() -> None:
    await asyncio.sleep(EGRESS_PROBE_INITIAL_DELAY_SECONDS)
    while True:
        try:
            await refresh_egress_state()
        except asyncio.CancelledError:
            raise
        except Exception:  # never let the canary take the renderer down
            _egress_state.update(enforced=None, checked_at=datetime.now(timezone.utc).isoformat())
        await asyncio.sleep(EGRESS_PROBE_INTERVAL_SECONDS)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    task = asyncio.create_task(_egress_canary_loop(), name="renderer-egress-canary")
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(
    title="GreenKube Isolated Renderer",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
async def status() -> dict[str, Any]:
    """Egress canary result (F6), read by the controller's readiness reconciler."""
    return {"status": "ok", "egress": dict(_egress_state)}


@app.post("/render")
async def render(request: Request) -> dict[str, Any]:
    metadata = _decode_metadata(request.headers.get("x-gk-render-request"))
    archive_data = await _read_bounded_body(request)
    try:
        snapshot = extract_archive(
            archive_data,
            provider_id="isolated-renderer",
            repo_url="https://renderer.invalid/snapshot.git",
            commit_sha="0" * 40,
            limits=_SNAPSHOT_LIMITS,
        )
    except SnapshotLimitError:
        raise HTTPException(
            status_code=413,
            detail="Renderer snapshot archive exceeds its extraction limits.",
        ) from None
    except SnapshotError:
        raise HTTPException(
            status_code=400,
            detail="Renderer snapshot archive is invalid or exceeds its limits.",
        ) from None

    with snapshot:
        cwd = _resolve_cwd(snapshot.workspace, metadata.cwd)
        command = _resolve_command(metadata.argv, snapshot.workspace)
        requested_timeout = _bounded_number(metadata.timeout_seconds, _MAX_TIMEOUT_SECONDS)
        max_stdout_bytes = _bounded_limit(metadata.max_stdout_bytes, _MAX_STDOUT_BYTES)
        max_stderr_bytes = _bounded_limit(metadata.max_stderr_bytes, _MAX_STDERR_BYTES)
        prlimit = shutil.which("prlimit")
        if prlimit is None:
            raise HTTPException(status_code=503, detail="Renderer resource-limit utility is unavailable.")
        bounded_command = (
            prlimit,
            f"--as={_MAX_ADDRESS_SPACE_BYTES}",
            f"--cpu={max(1, math.ceil(requested_timeout))}",
            f"--nproc={_MAX_PROCESS_COUNT}",
            "--nofile=256",
            f"--fsize={_MAX_FILE_BYTES}",
            "--",
            *command,
        )
        async with _RENDER_SLOTS:
            try:
                result = await asyncio.to_thread(
                    BoundedSubprocessRunner().run,
                    bounded_command,
                    workspace=snapshot.workspace,
                    cwd=cwd,
                    timeout_seconds=requested_timeout,
                    max_stdout_bytes=max_stdout_bytes,
                    max_stderr_bytes=max_stderr_bytes,
                )
            except SandboxError:
                raise HTTPException(status_code=500, detail="Isolated renderer execution failed.") from None

    return {
        "returncode": result.returncode,
        "stdout": base64.b64encode(result.stdout).decode("ascii"),
        "stderr": base64.b64encode(result.stderr).decode("ascii"),
        "duration_seconds": result.duration_seconds,
    }


async def _read_bounded_body(request: Request) -> bytes:
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > _SNAPSHOT_LIMITS.max_compressed_bytes:
            raise HTTPException(status_code=413, detail="Renderer snapshot archive exceeds its size limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_metadata(header: str | None) -> RenderRequest:
    if not isinstance(header, str) or not header or len(header) > _MAX_REQUEST_HEADER_BYTES:
        raise HTTPException(status_code=400, detail="Renderer command metadata is missing or too large.")
    try:
        decoded = base64.urlsafe_b64decode(header.encode("ascii"))
        payload = json.loads(decoded)
        request = RenderRequest.model_validate(payload)
    except (UnicodeEncodeError, ValueError, ValidationError):
        raise HTTPException(status_code=400, detail="Renderer command metadata is invalid.") from None
    if any(not isinstance(argument, str) or not argument or "\x00" in argument for argument in request.argv):
        raise HTTPException(status_code=400, detail="Renderer arguments are invalid.")
    if any(len(argument.encode("utf-8")) > _MAX_ARGUMENT_BYTES for argument in request.argv):
        raise HTTPException(status_code=400, detail="Renderer argument exceeds its size limit.")
    return request


def _resolve_cwd(workspace: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise HTTPException(status_code=400, detail="Renderer working directory is invalid.")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".."} for part in relative.parts):
        if value != ".":
            raise HTTPException(status_code=400, detail="Renderer working directory escapes its snapshot.")
    path = (workspace / relative).resolve()
    try:
        path.relative_to(workspace.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Renderer working directory escapes its snapshot.") from None
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Renderer working directory is not a directory.")
    return path


def _resolve_command(argv: list[str], workspace: Path) -> tuple[str, ...]:
    executable = Path(argv[0]).name
    if executable not in _ALLOWED_RENDERERS:
        raise HTTPException(status_code=400, detail="Only the pinned Helm and Kustomize commands are supported.")
    binary = shutil.which(executable)
    if binary is None:
        raise HTTPException(status_code=503, detail=f"The pinned {executable} renderer is unavailable.")
    return (binary, *(_restore_workspace_argument(argument, workspace) for argument in argv[1:]))


def _restore_workspace_argument(argument: str, workspace: Path) -> str:
    if argument == _WORKSPACE_MARKER:
        return str(workspace)
    if argument.startswith(f"{_WORKSPACE_MARKER}/"):
        relative = argument.removeprefix(f"{_WORKSPACE_MARKER}/")
        return str(_safe_workspace_path(workspace, relative))
    option, separator, value = argument.partition("=")
    if separator and value.startswith(f"{_WORKSPACE_MARKER}/"):
        relative = value.removeprefix(f"{_WORKSPACE_MARKER}/")
        return f"{option}={_safe_workspace_path(workspace, relative)}"
    if argument.startswith("/"):
        raise HTTPException(status_code=400, detail="Absolute renderer paths must be inside the snapshot.")
    return argument


def _safe_workspace_path(workspace: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or "\\" in relative or any(part in {"", ".."} for part in path.parts):
        raise HTTPException(status_code=400, detail="Renderer file path escapes its snapshot.")
    resolved = (workspace / path).resolve()
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Renderer file path escapes its snapshot.") from None
    return resolved


def _bounded_number(value: object, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise HTTPException(status_code=400, detail="Renderer timeout is invalid.")
    return min(float(value), maximum)


def _bounded_limit(value: object, maximum: int) -> int:
    if type(value) is not int or value <= 0:
        raise HTTPException(status_code=400, detail="Renderer output limit is invalid.")
    return min(value, maximum)


if __name__ == "__main__":
    run(app, host="0.0.0.0", port=int(os.getenv("RENDERER_PORT", "8090")), log_level="warning")
