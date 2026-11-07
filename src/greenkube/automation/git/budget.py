# src/greenkube/automation/git/budget.py
"""Per-operation Git provider call budgets and their Prometheus metrics.

Every HTTP request a :class:`~greenkube.automation.git.base.GitProvider` makes
is charged to the budget active in the current asyncio context. A worker opens
one budget around each plan operation, so opening a pull request has a hard,
observable upper bound on provider calls that does not depend on repository
size (roadmap D5, BUG-47). Requests outside a budget are still counted.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from prometheus_client import Counter, Histogram

from greenkube.core.observability import REGISTRY

DEFAULT_PLAN_OPERATION_PROVIDER_CALL_BUDGET = 200
DEFAULT_SYNC_ANCESTRY_PROVIDER_CALL_BUDGET = 10

_OPERATIONS = frozenset({"plan_apply", "plan_rollback", "plan_unknown", "sync_ancestry", "unscoped"})
_PROVIDERS = frozenset({"github", "gitlab", "gitea"})

GIT_PROVIDER_REQUESTS = Counter(
    "greenkube_git_provider_requests_total",
    "HTTP requests sent to Git provider APIs, including retries.",
    ["provider", "operation"],
    registry=REGISTRY,
)
GIT_PROVIDER_OPERATION_CALLS = Histogram(
    "greenkube_git_provider_operation_calls",
    "Git provider API calls made by one budgeted operation.",
    ["operation"],
    buckets=(1, 2, 5, 10, 15, 20, 30, 50, 100, 200, 500),
    registry=REGISTRY,
)
GIT_PROVIDER_BUDGET_EXHAUSTED = Counter(
    "greenkube_git_provider_budget_exhausted_total",
    "Operations stopped because they exceeded their Git provider call budget.",
    ["operation"],
    registry=REGISTRY,
)


@dataclass(slots=True)
class ProviderCallBudget:
    """Mutable call counter for one operation; ``limit`` is inclusive."""

    operation: str
    limit: int
    calls: int = 0
    exhausted: bool = False


_CURRENT_BUDGET: ContextVar[ProviderCallBudget | None] = ContextVar("greenkube_git_provider_budget", default=None)


def _operation_label(operation: str) -> str:
    return operation if operation in _OPERATIONS else "unscoped"


def _provider_label(provider: str) -> str:
    return provider if provider in _PROVIDERS else "other"


@contextmanager
def provider_call_budget(operation: str, *, limit: int) -> Generator[ProviderCallBudget, None, None]:
    """Charge every provider request made in this context to one bounded budget."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("provider call budget limit must be a positive integer")
    budget = ProviderCallBudget(operation=_operation_label(operation), limit=limit)
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_BUDGET.reset(token)
        GIT_PROVIDER_OPERATION_CALLS.labels(operation=budget.operation).observe(budget.calls)


def current_provider_call_budget() -> ProviderCallBudget | None:
    """Return the budget charged by provider requests in this context, if any."""
    return _CURRENT_BUDGET.get()


def charge_provider_call(provider: str) -> None:
    """Count one provider HTTP request and fail closed once the budget is spent.

    Raises :class:`ProviderCallBudgetExceededError` (a non-transient
    ``GitProviderError``) before the request is sent when the active budget is
    exhausted, so a runaway operation cannot burn the provider rate limit.
    """
    budget = _CURRENT_BUDGET.get()
    operation = budget.operation if budget is not None else "unscoped"
    if budget is not None and budget.calls >= budget.limit:
        if not budget.exhausted:
            budget.exhausted = True
            GIT_PROVIDER_BUDGET_EXHAUSTED.labels(operation=operation).inc()
        from greenkube.automation.git.base import ProviderCallBudgetExceededError

        raise ProviderCallBudgetExceededError(
            f"Git provider call budget of {budget.limit} calls for {operation} was exhausted; "
            "refusing further provider requests for this operation."
        )
    GIT_PROVIDER_REQUESTS.labels(provider=_provider_label(provider), operation=operation).inc()
    if budget is not None:
        budget.calls += 1
