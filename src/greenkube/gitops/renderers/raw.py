"""Safe, deterministic renderer for raw Kubernetes YAML and JSON manifests."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from greenkube.gitops.contracts import RendererKind, ResourceTarget

_VOLATILE_LABEL_KEYS = frozenset({"controller-revision-hash", "pod-template-hash"})
_VOLATILE_ANNOTATION_KEYS = frozenset(
    {
        "deployment.kubernetes.io/revision",
        "kubectl.kubernetes.io/last-applied-configuration",
    }
)


@dataclass(frozen=True, slots=True)
class RenderLimits:
    """Hard limits applied before and during parsing untrusted input."""

    max_file_bytes: int = 1_048_576
    max_total_bytes: int = 4_194_304
    max_documents: int = 256
    max_nodes: int = 100_000
    max_depth: int = 100

    def __post_init__(self) -> None:
        for name, value in (
            ("max_file_bytes", self.max_file_bytes),
            ("max_total_bytes", self.max_total_bytes),
            ("max_documents", self.max_documents),
            ("max_nodes", self.max_nodes),
            ("max_depth", self.max_depth),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class RenderedResource:
    """One normalized Kubernetes resource and the raw document that produced it."""

    target: ResourceTarget
    manifest: dict[str, Any]
    source_path: str
    document_index: int


@dataclass(frozen=True, slots=True)
class RawRenderResult:
    """Normalized renderer output, deterministic digest, and non-fatal warnings."""

    renderer: RendererKind
    resources: tuple[RenderedResource, ...]
    warnings: tuple[str, ...]
    output_digest: str


class RawRenderError(ValueError):
    """Raw source is invalid, unsafe, or has an ambiguous resource identity."""


class RawRenderLimitError(RawRenderError):
    """Raw source exceeded a configured parsing or document limit."""


class _DuplicateJSONKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class _InvalidJSONConstant(ValueError):
    def __init__(self, constant: str) -> None:
        super().__init__(constant)
        self.constant = constant


@dataclass(slots=True)
class _NormalizationState:
    nodes: int = 0
    active_containers: set[int] = field(default_factory=set)


class RawRenderer:
    """Render a set of repository-relative raw Kubernetes source files."""

    def __init__(self, limits: RenderLimits | None = None) -> None:
        self.limits = limits or RenderLimits()

    def render(self, sources: Mapping[str, str | bytes]) -> RawRenderResult:
        if not isinstance(sources, Mapping):
            raise RawRenderError("sources must be a mapping of relative paths to YAML or JSON content")
        if not sources:
            raise RawRenderError("at least one source file is required")

        prepared_sources = self._prepare_sources(sources)
        resources: list[RenderedResource] = []
        warnings: list[str] = []
        seen: dict[tuple[str, str, str, str], RenderedResource] = {}
        state = _NormalizationState()
        document_count = 0

        for source_path, content, is_json in prepared_sources:
            documents = (
                self._json_document(source_path, content) if is_json else self._yaml_documents(source_path, content)
            )
            found_document = False

            for document_index, raw_document in enumerate(documents):
                found_document = True
                if document_count >= self.limits.max_documents:
                    raise RawRenderLimitError(f"{source_path}: document limit of {self.limits.max_documents} exceeded")
                document_count += 1

                context = f"{source_path} document index {document_index}"
                if raw_document is None:
                    if is_json:
                        raise RawRenderError(f"{context}: JSON document must be a resource object")
                    warnings.append(f"{context} is empty and was skipped")
                    continue

                document = _normalize_value(
                    raw_document,
                    context=context,
                    limits=self.limits,
                    state=state,
                    depth=0,
                    value_path="$",
                )
                if not isinstance(document, dict):
                    raise RawRenderError(f"{context}: Kubernetes document must be a mapping")

                _required_identity_value(document, "apiVersion", context)
                kind = _required_identity_value(document, "kind", context)
                if kind == "List":
                    items = document.get("items")
                    if not isinstance(items, list):
                        raise RawRenderError(f"{context}: kind: List requires an items list")
                    if not items:
                        warnings.append(f"{context}: empty kind: List produced no resources")
                    for item_index, item in enumerate(items):
                        item_context = f"{context} List item index {item_index}"
                        if not isinstance(item, dict):
                            raise RawRenderError(f"{item_context}: resource must be a mapping")
                        self._append_resource(
                            item,
                            source_path=source_path,
                            document_index=document_index,
                            context=item_context,
                            seen=seen,
                            resources=resources,
                            warnings=warnings,
                        )
                    continue

                self._append_resource(
                    document,
                    source_path=source_path,
                    document_index=document_index,
                    context=context,
                    seen=seen,
                    resources=resources,
                    warnings=warnings,
                )

            if not found_document and not is_json:
                warnings.append(f"{source_path} contains no YAML documents")

        return RawRenderResult(
            renderer=RendererKind.RAW,
            resources=tuple(resources),
            warnings=tuple(warnings),
            output_digest=_output_digest(resources),
        )

    def _prepare_sources(self, sources: Mapping[str, str | bytes]) -> list[tuple[str, str, bool]]:
        entries: list[tuple[str, str | bytes]] = []
        for path, content in sources.items():
            _validate_source_path(path)
            entries.append((path, content))

        entries.sort(key=lambda entry: entry[0])
        prepared: list[tuple[str, str, bool]] = []
        total_bytes = 0
        for path, content in entries:
            if not isinstance(content, (str, bytes)):
                raise RawRenderError(f"{path}: source content must be str or bytes")
            minimum_byte_count = len(content)
            if minimum_byte_count > self.limits.max_file_bytes:
                raise RawRenderLimitError(f"{path}: file byte limit of {self.limits.max_file_bytes} exceeded")
            if total_bytes + minimum_byte_count > self.limits.max_total_bytes:
                raise RawRenderLimitError(f"total input byte limit of {self.limits.max_total_bytes} exceeded")

            if isinstance(content, str):
                try:
                    encoded = content.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise RawRenderError(f"{path}: source content must be valid UTF-8") from exc
            else:
                encoded = content

            byte_count = len(encoded)
            if byte_count > self.limits.max_file_bytes:
                raise RawRenderLimitError(f"{path}: file byte limit of {self.limits.max_file_bytes} exceeded")
            total_bytes += byte_count
            if total_bytes > self.limits.max_total_bytes:
                raise RawRenderLimitError(f"total input byte limit of {self.limits.max_total_bytes} exceeded")

            try:
                decoded = encoded.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RawRenderError(f"{path}: source content must be valid UTF-8") from exc

            is_json = PurePosixPath(path).suffix.lower() == ".json"
            prepared.append((path, decoded, is_json))
        return prepared

    @staticmethod
    def _json_document(source_path: str, content: str) -> tuple[Any, ...]:
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise _DuplicateJSONKey(key)
                result[key] = value
            return result

        def reject_constant(constant: str) -> None:
            raise _InvalidJSONConstant(constant)

        try:
            document = json.loads(
                content,
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
        except _DuplicateJSONKey as exc:
            raise RawRenderError(f"{source_path}: duplicate JSON object key {exc.key!r}") from exc
        except _InvalidJSONConstant as exc:
            raise RawRenderError(f"{source_path}: non-standard JSON number {exc.constant!r}") from exc
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise RawRenderError(f"{source_path}: invalid JSON ({type(exc).__name__})") from exc
        return (document,)

    @staticmethod
    def _yaml_documents(source_path: str, content: str) -> Iterator[Any]:
        parser = YAML(typ="safe", pure=True)
        parser.version = (1, 2)
        parser.allow_duplicate_keys = False
        try:
            yield from parser.load_all(content)
        except (YAMLError, RecursionError, ValueError, TypeError) as exc:
            raise RawRenderError(f"{source_path}: invalid YAML ({type(exc).__name__})") from exc

    def _append_resource(
        self,
        manifest: dict[str, Any],
        *,
        source_path: str,
        document_index: int,
        context: str,
        seen: dict[tuple[str, str, str, str], RenderedResource],
        resources: list[RenderedResource],
        warnings: list[str],
    ) -> None:
        api_version = _required_identity_value(manifest, "apiVersion", context)
        kind = _required_identity_value(manifest, "kind", context)
        if kind == "List":
            raise RawRenderError(f"{context}: nested kind: List is not supported")

        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            raise RawRenderError(f"{context}: metadata must be a mapping")
        name = _required_identity_value(metadata, "name", context, prefix="metadata.")
        namespace_value = metadata.get("namespace", "")
        if not isinstance(namespace_value, str) or namespace_value != namespace_value.strip():
            raise RawRenderError(f"{context}: metadata.namespace must be a string without surrounding whitespace")

        target = ResourceTarget(
            api_version=api_version,
            kind=kind,
            namespace=namespace_value,
            name=name,
        )
        identity = (target.api_version, target.kind, target.namespace, target.name)
        previous = seen.get(identity)
        if previous is not None:
            raise RawRenderError(
                f"{context}: duplicate resource identity {identity!r}; already emitted from "
                f"{previous.source_path} document index {previous.document_index}"
            )

        normalized_manifest = _strip_volatile_metadata(manifest, context=context, warnings=warnings)
        resource = RenderedResource(
            target=target,
            manifest=normalized_manifest,
            source_path=source_path,
            document_index=document_index,
        )
        seen[identity] = resource
        resources.append(resource)


def _validate_source_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or path != path.strip()
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
    ):
        raise RawRenderError("source path must be a non-empty relative POSIX path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise RawRenderError(f"source path {path!r} must not contain empty, '.' or '..' components")


def _required_identity_value(
    mapping: dict[str, Any],
    key: str,
    context: str,
    *,
    prefix: str = "",
) -> str:
    field_name = f"{prefix}{key}"
    value = mapping.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise RawRenderError(f"{context}: {field_name} must be a non-empty string without surrounding whitespace")
    return value


def _normalize_value(
    value: Any,
    *,
    context: str,
    limits: RenderLimits,
    state: _NormalizationState,
    depth: int,
    value_path: str,
) -> Any:
    if depth > limits.max_depth:
        raise RawRenderLimitError(f"{context}: nesting depth limit of {limits.max_depth} exceeded at {value_path}")

    state.nodes += 1
    if state.nodes > limits.max_nodes:
        raise RawRenderLimitError(f"{context}: node limit of {limits.max_nodes} exceeded")

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RawRenderError(f"{context}: non-finite number at {value_path}")
        return value

    if isinstance(value, Mapping):
        active_containers = state.active_containers
        value_id = id(value)
        if value_id in active_containers:
            raise RawRenderError(f"{context}: recursive YAML alias at {value_path}")
        if any(not isinstance(key, str) for key in value):
            raise RawRenderError(f"{context}: mapping keys must be strings at {value_path}")
        active_containers.add(value_id)
        try:
            return {
                key: _normalize_value(
                    value[key],
                    context=context,
                    limits=limits,
                    state=state,
                    depth=depth + 1,
                    value_path=f"{value_path}.{key}",
                )
                for key in sorted(value)
            }
        finally:
            active_containers.remove(value_id)

    if isinstance(value, list):
        active_containers = state.active_containers
        value_id = id(value)
        if value_id in active_containers:
            raise RawRenderError(f"{context}: recursive YAML alias at {value_path}")
        active_containers.add(value_id)
        try:
            return [
                _normalize_value(
                    item,
                    context=context,
                    limits=limits,
                    state=state,
                    depth=depth + 1,
                    value_path=f"{value_path}[{index}]",
                )
                for index, item in enumerate(value)
            ]
        finally:
            active_containers.remove(value_id)

    raise RawRenderError(f"{context}: unsupported YAML/JSON value type {type(value).__name__} at {value_path}")


def _strip_volatile_metadata(
    manifest: dict[str, Any],
    *,
    context: str,
    warnings: list[str],
) -> dict[str, Any]:
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        return manifest

    for field_name, ignored_keys in (
        ("labels", _VOLATILE_LABEL_KEYS),
        ("annotations", _VOLATILE_ANNOTATION_KEYS),
    ):
        if field_name not in metadata:
            continue
        values = metadata[field_name]
        if not isinstance(values, dict):
            raise RawRenderError(f"{context}: metadata.{field_name} must be a mapping")

        removed = sorted(ignored_keys.intersection(values))
        for key in removed:
            del values[key]
            warnings.append(f"{context}: ignored volatile metadata key {key!r}")
        if removed and not values:
            del metadata[field_name]

    return manifest


def _output_digest(resources: list[RenderedResource]) -> str:
    normalized_resources = sorted(
        resources,
        key=lambda resource: (
            resource.target.api_version,
            resource.target.kind,
            resource.target.namespace,
            resource.target.name,
        ),
    )
    digest_payload = [
        {
            "identity": {
                "api_version": resource.target.api_version,
                "kind": resource.target.kind,
                "namespace": resource.target.namespace,
                "name": resource.target.name,
            },
            "manifest": resource.manifest,
        }
        for resource in normalized_resources
    ]
    canonical = json.dumps(
        digest_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    value = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{value}"


def render_raw(
    sources: Mapping[str, str | bytes],
    *,
    limits: RenderLimits | None = None,
) -> RawRenderResult:
    """Convenience function for rendering a mapping of repository paths to file content."""

    return RawRenderer(limits=limits).render(sources)
