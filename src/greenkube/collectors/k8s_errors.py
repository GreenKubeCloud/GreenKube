"""Typed errors and paginated list calls of the Kubernetes collectors (WP-01.2, COL-07)."""

from typing import Any, Awaitable, Callable, List

#: Objects per Kubernetes list page: one unbounded list of every pod times out on large clusters.
K8S_LIST_PAGE_SIZE = 500


class KubernetesListError(RuntimeError):
    """A Kubernetes list call failed: the result is unknown, not empty."""


async def list_all_pages(
    list_call: Callable[..., Awaitable[Any]], *, page_size: int = K8S_LIST_PAGE_SIZE, **kwargs: Any
) -> List[Any]:
    """Every item of a Kubernetes list call, following ``continue`` tokens page by page.

    ``kwargs`` are passed to every page request (``watch``, ``_request_timeout``).
    Any failure of any page propagates: a partial list is never returned.
    """
    items: List[Any] = []
    token: str | None = None
    while True:
        request: dict[str, Any] = dict(kwargs, limit=page_size)
        if token:
            request["_continue"] = token
        page = await list_call(**request)
        items.extend(getattr(page, "items", None) or [])
        metadata = getattr(page, "metadata", None)
        next_token = getattr(metadata, "_continue", None) if metadata is not None else None
        if not isinstance(next_token, str) or not next_token or next_token == token:
            return items
        token = next_token
