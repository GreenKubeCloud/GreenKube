"""Source registry: built-ins, entry-point plugins and per-source configuration (roadmap E5).

* Built-in sources are registered in code
  (:func:`greenkube.core.optimization.registry.builtin_source_classes`).
* Third-party sources are Python packages that declare an entry point in the
  ``greenkube.sources`` group. The entry point resolves to a
  :class:`~greenkube.signals.sdk.Source` subclass, or to a callable returning
  several. Plugins load when ``SOURCES_PLUGINS_ENABLED`` is true (default).
* ``SOURCES_CONFIG`` is a JSON object keyed by source id; each value is
  validated against that source's ``settings_model`` and may set ``enabled``
  to ``true``, ``false`` or ``auto``.

Every problem (a plugin that does not import, an id collision, invalid
settings, settings for an unknown source) is recorded on the registry and
reported by ``GET /api/v1/sources``; it never prevents other sources from
running.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import ValidationError

from greenkube.signals.sdk import (
    ENTRY_POINT_GROUP,
    PLUGIN_ORIGIN,
    EnabledMode,
    Source,
    SourceDeps,
    SourceSettings,
    validate_source_class,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

#: Maximum length of a problem message kept for the API.
_MESSAGE_LIMIT = 300


def parse_sources_config(value: Any) -> Dict[str, Dict[str, Any]]:
    """Parse ``SOURCES_CONFIG``: a JSON object of objects keyed by source id."""
    if value is None or value == "":
        return {}
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("SOURCES_CONFIG must be a JSON object keyed by source id")
    result: Dict[str, Dict[str, Any]] = {}
    for key, settings in parsed.items():
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError(f"SOURCES_CONFIG['{key}'] must be an object")
        settings = dict(settings)
        if "enabled" in settings and isinstance(settings["enabled"], bool):
            settings["enabled"] = "true" if settings["enabled"] else "false"
        result[str(key)] = settings
    return result


@dataclass(frozen=True)
class SourceDescriptor:
    """A registered source class and where it comes from."""

    source_class: type[Source]
    origin: str = "builtin"
    entry_point: Optional[str] = None
    distribution: Optional[str] = None

    @property
    def id(self) -> str:
        return self.source_class.id


@dataclass(frozen=True)
class RegistryProblem:
    """Something the registry could not honour, reported by the sources API."""

    code: str
    message: str
    source_id: Optional[str] = None
    entry_point: Optional[str] = None


@dataclass
class ConfiguredSource:
    """A descriptor with its resolved enablement and validated settings."""

    descriptor: SourceDescriptor
    enabled: EnabledMode
    settings: Optional[SourceSettings] = None
    error: Optional[str] = None

    @property
    def id(self) -> str:
        return self.descriptor.id


def _message(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _MESSAGE_LIMIT else text[: _MESSAGE_LIMIT - 1] + "…"


def _entry_point_sources(loaded: Any) -> List[Any]:
    if isinstance(loaded, type):
        return [loaded]
    if callable(loaded):
        produced: Any = loaded()
        if isinstance(produced, type):
            return [produced]
        return list(produced)
    raise ValueError("entry point must resolve to a Source subclass or a callable returning Source subclasses")


EntryPointsFn = Callable[[], Iterable[Any]]


def _default_entry_points() -> Iterable[Any]:
    return metadata.entry_points(group=ENTRY_POINT_GROUP)


class SourceRegistry:
    """Known sources (built-in first, then plugins) and the problems met while loading them."""

    def __init__(self) -> None:
        self._descriptors: Dict[str, SourceDescriptor] = {}
        self.problems: List[RegistryProblem] = []

    def register(self, source_class: type[Source], *, origin: str = "builtin", **extra: Any) -> None:
        validate_source_class(source_class, builtin=origin == "builtin")
        source_id = source_class.id
        if source_id in self._descriptors:
            raise ValueError(f"source id {source_id!r} is already registered")
        self._descriptors[source_id] = SourceDescriptor(source_class=source_class, origin=origin, **extra)

    def load_plugins(self, entry_points: Optional[EntryPointsFn] = None) -> None:
        """Register third-party sources; a broken plugin is recorded, never raised."""
        try:
            points = list((entry_points or _default_entry_points)())
        except Exception as exc:
            self.problems.append(RegistryProblem(code="entry_points_unreadable", message=_message(exc)))
            return
        for point in points:
            name = f"{getattr(point, 'name', '?')} = {getattr(point, 'value', '?')}"
            dist = getattr(getattr(point, "dist", None), "name", None)
            try:
                classes = _entry_point_sources(point.load())
                for source_class in classes:
                    self.register(source_class, origin=PLUGIN_ORIGIN, entry_point=name, distribution=dist)
            except Exception as exc:
                logger.error("Could not load source plugin %s: %s", name, _message(exc))
                self.problems.append(
                    RegistryProblem(code="plugin_load_failed", message=_message(exc), entry_point=name)
                )

    def get(self, source_id: str) -> Optional[SourceDescriptor]:
        return self._descriptors.get(source_id)

    def descriptors(self) -> List[SourceDescriptor]:
        return list(self._descriptors.values())

    def configure(
        self,
        settings_by_id: Dict[str, Dict[str, Any]],
        defaults: Optional[Dict[str, EnabledMode]] = None,
    ) -> List[ConfiguredSource]:
        """Resolve enablement and validate settings for every registered source.

        Precedence for ``enabled``: ``SOURCES_CONFIG[<id>].enabled``, then
        ``defaults`` (legacy flags such as ``RECOMMENDATION_VPA_ENABLED``), then
        the source class's ``default_enabled``.
        """
        defaults = defaults or {}
        configured: List[ConfiguredSource] = []
        for descriptor in self._descriptors.values():
            cls = descriptor.source_class
            raw = dict(settings_by_id.get(descriptor.id, {}))
            try:
                settings = cls.settings_model.model_validate(raw)
            except ValidationError as exc:
                errors = "; ".join(
                    f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}" for err in exc.errors()
                )
                configured.append(
                    ConfiguredSource(
                        descriptor=descriptor,
                        enabled=EnabledMode(raw["enabled"])
                        if raw.get("enabled") in ("true", "false", "auto")
                        else cls.default_enabled,
                        error=f"invalid settings: {errors}"[:_MESSAGE_LIMIT],
                    )
                )
                continue
            enabled = settings.enabled or defaults.get(descriptor.id) or cls.default_enabled
            configured.append(ConfiguredSource(descriptor=descriptor, enabled=EnabledMode(enabled), settings=settings))
        for unknown in sorted(set(settings_by_id) - set(self._descriptors)):
            self.problems.append(
                RegistryProblem(
                    code="unknown_source",
                    message=f"SOURCES_CONFIG names '{unknown}', which is not a registered source",
                    source_id=unknown,
                )
            )
        return configured


def instantiate(configured: Sequence[ConfiguredSource], deps: SourceDeps) -> Tuple[List[Source], Dict[str, str]]:
    """Create the enabled, valid sources; a constructor that raises is reported, not propagated."""
    sources: List[Source] = []
    errors: Dict[str, str] = {}
    for item in configured:
        if item.enabled == EnabledMode.FALSE or item.error is not None or item.settings is None:
            continue
        try:
            source = item.descriptor.source_class(item.settings, deps)
            # Set after construction so the source cannot choose its own origin.
            source.registry_origin = item.descriptor.origin
            sources.append(source)
        except Exception as exc:
            logger.exception("Could not create source '%s'; continuing without it.", item.id)
            errors[item.id] = _message(exc)
    return sources, errors
