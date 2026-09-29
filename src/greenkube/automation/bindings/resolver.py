"""Priority-aware repository binding resolution."""

from __future__ import annotations

from collections.abc import Iterable

from greenkube.models.repository_binding import (
    BindingResolution,
    BindingSource,
    BindingStatus,
    RepositoryBinding,
)


class RepositoryBindingResolver:
    """Resolve bindings without silently choosing between equal candidates."""

    _SOURCE_PRIORITY = {
        BindingSource.ARGOCD: 300,
        BindingSource.FLUX: 200,
        BindingSource.ANNOTATION: 100,
    }

    def resolve(self, candidates: Iterable[RepositoryBinding]) -> BindingResolution:
        items = list(candidates)
        if not items:
            return BindingResolution(status=BindingStatus.UNRESOLVED, reason="No repository binding candidates found")

        ranked = sorted(items, key=lambda item: (item.priority, self._SOURCE_PRIORITY[item.source]), reverse=True)
        highest = ranked[0]
        tied = [
            item
            for item in ranked
            if (item.priority, self._SOURCE_PRIORITY[item.source])
            == (highest.priority, self._SOURCE_PRIORITY[highest.source])
        ]
        identities = {(item.repo_url, item.path, item.branch) for item in tied}
        if len(identities) > 1:
            return BindingResolution(
                status=BindingStatus.AMBIGUOUS,
                candidates=ranked,
                reason="Multiple repository bindings have the same priority",
            )
        return BindingResolution(status=BindingStatus.RESOLVED, binding=highest, candidates=ranked)

    def resolve_many(self, candidates: Iterable[RepositoryBinding]) -> dict[tuple[str, str, str], BindingResolution]:
        grouped: dict[tuple[str, str, str], list[RepositoryBinding]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.workload_key, []).append(candidate)
        return {key: self.resolve(items) for key, items in grouped.items()}

    def coverage(
        self,
        workloads: Iterable[tuple[str, str, str]],
        candidates: Iterable[RepositoryBinding],
    ) -> float:
        """Return the fraction of workloads with one unambiguous binding."""

        workload_keys = set(workloads)
        if not workload_keys:
            return 1.0
        resolutions = self.resolve_many(candidates)
        resolved = sum(
            1
            for key in workload_keys
            if resolutions.get(key, BindingResolution(status=BindingStatus.UNRESOLVED)).status == BindingStatus.RESOLVED
        )
        return resolved / len(workload_keys)
