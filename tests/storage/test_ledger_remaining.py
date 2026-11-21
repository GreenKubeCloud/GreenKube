"""LED-12 regressions of the savings ledger on SQLite and PostgreSQL."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from greenkube.api.routers.ledger import window_share
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]  # noqa: F401
    CLUSTER,
    CLUSTER_UID,
    HOUR,
    LedgerDb,
    _draft,
    _hour,
    ledger,
)


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.parametrize("bounds", ["both", "start", "end"])
async def test_windowed_summary_equals_the_export_window_values(ledger: LedgerDb, bounds: str):  # noqa: F811
    """LED-12 (b): the summary and the export prorate every entry, zero-length ones too, by one rule."""
    base = _draft("x")
    drafts = [
        replace(base, entry_key="whole", group_key="whole", period_start=_hour(-5), period_end=_hour(-4)),
        replace(base, entry_key="straddle-start", group_key="s1", period_start=_hour(-7), period_end=_hour(-5)),
        replace(base, entry_key="straddle-end", group_key="s2", period_start=_hour(-3), period_end=_hour(-1)),
        replace(base, entry_key="zero-inside", group_key="z1", period_start=_hour(-4), period_end=_hour(-4)),
        replace(base, entry_key="zero-at-start", group_key="z2", period_start=_hour(-6), period_end=_hour(-6)),
        replace(base, entry_key="zero-at-end", group_key="z3", period_start=_hour(-2), period_end=_hour(-2)),
    ]
    await ledger.repo.append(CLUSTER_UID, CLUSTER, drafts)
    start = _hour(-6) if bounds in ("both", "start") else None
    end = _hour(-2) if bounds in ("both", "end") else None

    summary = math.fsum(row.value for row in await ledger.repo.summary(CLUSTER_UID, start=start, end=end))
    entries = await ledger.repo.list_entries(CLUSTER_UID, limit=100, start=start, end=end)
    exported = math.fsum(entry.value * window_share(entry, start, end) for entry in entries)
    assert summary == pytest.approx(exported, abs=1e-12)
