"""Migration 0300 retires the types of the removed advisory analyzers (roadmap B.1), SQLite and PostgreSQL.

The upgrade runs through the production runner over a database that holds
open and finished recommendations of the retired types next to a keyed savings
ledger chain: the open rows become stale with reason ``type_removed``, nothing
else changes and the ledger chain still verifies.
"""

from __future__ import annotations

import json

import pytest

from greenkube.core.migrations import MigrationRunner
from greenkube.models.metrics import RETIRED_RECOMMENDATION_TYPES, RecommendationType
from greenkube.storage.ledger_repository import LedgerRepository
from greenkube.verify.ledger.verification import verify_ledger
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]  # noqa: F401
    CLUSTER,
    CLUSTER_UID,
    LedgerDb,
    _draft,
    ledger,
)

pytestmark = pytest.mark.database

MIGRATION = 300
KEY = b"a-chain-key-from-a-kubernetes-secret"

#: (id, type, status, ignored_at) stored before the upgrade.
ROWS = (
    (1, "ZOMBIE_POD", "applied", None),
    (2, "ZOMBIE_POD", "active", None),
    (3, "OVERPROVISIONED_NODE", "ignored", "2026-11-02T00:00:00Z"),
    (4, "RIGHTSIZING_CPU", "active", None),
    (5, "IDLE_NAMESPACE", "stale", None),
    (6, "CARBON_AWARE_SCHEDULING", "active", None),
    (7, "ORPHANED_LOAD_BALANCER", "ignored", "2026-11-03T00:00:00Z"),
    (8, "OFF_PEAK_SCALING", "verified", None),
)


def test_the_migration_names_every_retired_type():
    for script in ("postgres", "sqlite"):
        sql = next(sql for version, _name, sql in MigrationRunner(script)._discover_scripts() if version == MIGRATION)
        assert {rec_type.value for rec_type in RETIRED_RECOMMENDATION_TYPES} == {
            rec_type.value for rec_type in RecommendationType if f"'{rec_type.value}'" in sql
        }


async def _rows(db: LedgerDb, sql: str) -> list[tuple]:
    async with db.manager.connection_scope() as conn:
        if db.backend == "postgres":
            return [tuple(row) for row in await conn.fetch(sql)]  # type: ignore[union-attr]
        cursor = await conn.execute(sql)
        return [tuple(row) for row in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_open_rows_of_retired_types_are_retired_and_the_ledger_chain_stays_valid(
    ledger: LedgerDb,  # noqa: F811
):
    # State of a 0.3 database: recommendation rows and a keyed ledger chain on the applied one.
    for row_id, rec_type, status, ignored_at in ROWS:
        ignored = "NULL" if ignored_at is None else f"'{ignored_at}'"
        await ledger.execute(
            "INSERT INTO recommendation_history (id, pod_name, namespace, type, description, status, fingerprint, "
            f"ignored_at, created_at) VALUES ({row_id}, 'api', 'shop', '{rec_type}', 'd', '{status}', "
            f"'fp-{row_id}', {ignored}, '2026-11-01T00:00:00Z')"
        )
    repo = LedgerRepository(ledger.manager, chain_key=KEY)
    await repo.append(CLUSTER_UID, CLUSTER, [_draft(f"zombie-{i}", 0.25) for i in range(6)])
    entries_before = await repo.list_entries(CLUSTER_UID, limit=100, ascending=True)
    assert (await verify_ledger(repo, checkpoints={}))[CLUSTER_UID].ok

    # Upgrade: the production runner applies 0300 again, now over this data.
    await ledger.execute(f"DELETE FROM schema_migrations WHERE version = {MIGRATION}")
    async with ledger.manager.connection_scope() as conn:
        assert await MigrationRunner(ledger.backend).run(conn) == 1

    statuses = dict(await _rows(ledger, "SELECT id, status FROM recommendation_history"))
    assert statuses == {
        1: "applied",
        2: "stale",
        3: "stale",
        4: "active",
        5: "stale",
        6: "stale",
        7: "ignored",
        8: "verified",
    }
    events = await _rows(
        ledger, "SELECT recommendation_id, actor, payload FROM recommendation_events WHERE event_type = 'stale'"
    )
    payloads = {row_id: (actor, json.loads(payload)) for row_id, actor, payload in events}
    assert payloads == {
        2: ("system", {"reason": "type_removed", "from_status": "active"}),
        3: ("system", {"reason": "type_removed", "from_status": "ignored"}),
        6: ("system", {"reason": "type_removed", "from_status": "active"}),
    }

    # The ledger is never rewritten: same entries, and the chain still verifies.
    entries_after = await repo.list_entries(CLUSTER_UID, limit=100, ascending=True)
    assert [(entry.seq, entry.hash, entry.value) for entry in entries_after] == [
        (entry.seq, entry.hash, entry.value) for entry in entries_before
    ]
    assert (await verify_ledger(repo, checkpoints={}, full=True))[CLUSTER_UID].ok
