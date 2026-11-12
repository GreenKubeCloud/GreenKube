"""Values-path mapping registry for Helm charts (H2).

A mapping names where a chart reads one container's ``resources`` in its values
(``resources``, ``metrics.resources``, ``controller.resources``...). Rules come
from the operator's configuration (``automation.helm.chartMappings``, per chart and
version) first, then from the built-in registry of well-known charts. A rule only
proposes candidate paths: the planner still render-probes each candidate and keeps
the first one that changes exactly the requested container, so a stale or wrong
rule fails closed instead of producing an unrelated edit.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_MAX_PATH_DEPTH = 32
_KEY = re.compile(r"^[^\s.]+$")


class ChartMappingConfigError(ValueError):
    """A configured chart mapping is malformed."""


@dataclass(frozen=True, slots=True)
class ChartIdentity:
    """What a mapping rule can match: the chart's own metadata and where it came from."""

    name: str
    version: str
    repository: str | None = None
    dependencies: tuple[tuple[str, str], ...] = ()

    @property
    def qualified_names(self) -> tuple[str, ...]:
        names = [self.name]
        if self.repository:
            names.append(f"{self.repository.rstrip('/')}/{self.name}")
        return tuple(names)


@dataclass(frozen=True, slots=True)
class ChartMappingRule:
    """Candidate values paths for the containers of the charts a rule matches."""

    chart: str
    paths: tuple[tuple[str, ...], ...]
    version: str = "*"
    container: str = "*"
    origin: str = "builtin"
    family: str | None = None

    def matches(self, identity: ChartIdentity, container: str) -> bool:
        if self.family is not None:
            predicate = _FAMILIES.get(self.family)
            if predicate is None or not predicate(identity):
                return False
        if not any(fnmatch.fnmatchcase(name, self.chart) for name in identity.qualified_names):
            return False
        if not fnmatch.fnmatchcase(identity.version.removeprefix("v"), self.version.removeprefix("v")):
            return False
        return fnmatch.fnmatchcase(container, self.container)

    def candidate_paths(self, container: str) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(container if key == "{container}" else key for key in path) for path in self.paths)


@dataclass(frozen=True, slots=True)
class MappingCandidate:
    path: tuple[str, ...]
    origin: str


def _is_bitnami(identity: ChartIdentity) -> bool:
    repository = (identity.repository or "").lower()
    if "bitnami" in repository:
        return True
    return any(
        name == "common" and "bitnami" in dep_repository.lower() for name, dep_repository in identity.dependencies
    )


_FAMILIES: dict[str, Callable[[ChartIdentity], bool]] = {"bitnami": _is_bitnami}


def _rule(
    chart: str,
    *paths: str,
    container: str = "*",
    family: str | None = None,
) -> ChartMappingRule:
    return ChartMappingRule(
        chart=chart,
        container=container,
        family=family,
        paths=tuple(tuple(path.split(".")) for path in paths),
    )


#: Well-known charts. Order matters: the first candidate that render-validates wins.
BUILTIN_CHART_MAPPINGS: tuple[ChartMappingRule, ...] = (
    # Bitnami charts (HTTP repository or oci://registry-1.docker.io/bitnamicharts): the
    # main container reads ``resources`` (falling back to ``resourcesPreset``), components
    # read ``<component>.resources`` and the exporter sidecar ``metrics.resources``.
    _rule(
        "*",
        "resources",
        "{container}.resources",
        "primary.resources",
        "master.resources",
        "controller.resources",
        "server.resources",
        "metrics.resources",
        family="bitnami",
    ),
    _rule("ingress-nginx", "controller.resources", container="controller"),
    _rule("ingress-nginx", "defaultBackend.resources", container="*backend*"),
    _rule("cert-manager", "resources", container="cert-manager-controller"),
    _rule("cert-manager", "webhook.resources", container="cert-manager-webhook"),
    _rule("cert-manager", "cainjector.resources", container="cert-manager-cainjector"),
    _rule("podinfo", "resources", container="podinfo"),
    _rule("grafana", "resources", container="grafana"),
    _rule("app-template", "controllers.main.containers.{container}.resources"),
    _rule("application", "deployment.resources"),
)


def parse_chart_mappings(raw: object) -> tuple[ChartMappingRule, ...]:
    """Parse ``GITOPS_HELM_CHART_MAPPINGS`` (a JSON list, or an already decoded list)."""
    if raw in (None, "", []):
        return ()
    value: Any = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ChartMappingConfigError("chart mappings must be a JSON list") from exc
    if not isinstance(value, list):
        raise ChartMappingConfigError("chart mappings must be a list")
    rules: list[ChartMappingRule] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ChartMappingConfigError(f"chart mapping {index} must be an object")
        unknown = set(item) - {"chart", "version", "container", "path", "paths"}
        if unknown:
            raise ChartMappingConfigError(f"chart mapping {index} has unknown keys: {', '.join(sorted(unknown))}")
        chart = item.get("chart")
        version = item.get("version", "*")
        container = item.get("container", "*")
        if not isinstance(chart, str) or not chart.strip():
            raise ChartMappingConfigError(f"chart mapping {index} needs a chart name")
        if not isinstance(version, str) or not version.strip():
            raise ChartMappingConfigError(f"chart mapping {index} version must be a version or '*'")
        if not isinstance(container, str) or not container.strip():
            raise ChartMappingConfigError(f"chart mapping {index} container must be a name or '*'")
        raw_paths: Any = item.get("paths") if "paths" in item else [item.get("path")]
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ChartMappingConfigError(f"chart mapping {index} needs a values path")
        rules.append(
            ChartMappingRule(
                chart=chart.strip(),
                version=version.strip(),
                container=container.strip(),
                paths=tuple(_values_path(path, index) for path in raw_paths),
                origin="config",
            )
        )
    return tuple(rules)


def _values_path(raw: object, index: int) -> tuple[str, ...]:
    if isinstance(raw, str):
        parts: Sequence[object] = raw.strip().split(".")
    elif isinstance(raw, list):
        parts = raw
    else:
        raise ChartMappingConfigError(f"chart mapping {index} path must be a dotted string or a list of keys")
    if not parts or len(parts) > _MAX_PATH_DEPTH:
        raise ChartMappingConfigError(f"chart mapping {index} path is empty or too deep")
    keys: list[str] = []
    for part in parts:
        if not isinstance(part, str) or not part or part != part.strip():
            raise ChartMappingConfigError(f"chart mapping {index} path keys must be non-blank strings")
        if isinstance(raw, str) and _KEY.fullmatch(part) is None:
            raise ChartMappingConfigError(f"chart mapping {index} path key {part!r} is invalid")
        keys.append(part)
    if keys[-1] != "resources":
        raise ChartMappingConfigError(f"chart mapping {index} path must end with 'resources'")
    return tuple(keys)


def mapping_candidates(
    identity: ChartIdentity,
    container: str,
    *,
    configured: Iterable[ChartMappingRule] = (),
    builtin: Iterable[ChartMappingRule] = BUILTIN_CHART_MAPPINGS,
) -> tuple[tuple[MappingCandidate, ...], bool]:
    """Ordered candidates for one container, and whether a configured rule matched.

    Configured rules come first and, when one matches, built-in rules are not used:
    the operator's mapping for a chart version is authoritative.
    """
    candidates: list[MappingCandidate] = []
    seen: set[tuple[str, ...]] = set()
    configured_match = False
    for rule in configured:
        if rule.matches(identity, container):
            configured_match = True
            for path in rule.candidate_paths(container):
                if path not in seen:
                    seen.add(path)
                    candidates.append(MappingCandidate(path=path, origin=f"configured chart mapping {rule.chart}"))
    if not configured_match:
        for rule in builtin:
            if rule.matches(identity, container):
                for path in rule.candidate_paths(container):
                    if path not in seen:
                        seen.add(path)
                        candidates.append(MappingCandidate(path=path, origin=f"built-in mapping for {identity.name}"))
    return tuple(candidates), configured_match
