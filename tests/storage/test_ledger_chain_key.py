"""Keyed hash chain of the savings ledger v2 (LED-05) on SQLite and PostgreSQL.

Reruns the audit's SIM4 (drop the triggers, multiply every cost, delete the
tail, re-chain and edit ``ledger_chain_status``) against a chain written with
``LEDGER_CHAIN_KEY``: without the key the attacker cannot recompute the
entries' HMACs. Entries written by earlier releases (no key) keep verifying.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import List

import pytest

from greenkube.storage.ledger_repository import ChainStatus, LedgerRepository
from greenkube.verify.ledger.model import LedgerEntry, chain_hash, chain_key_id
from greenkube.verify.ledger.verification import verify_ledger
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]  # noqa: F401
    CLUSTER,
    CLUSTER_UID,
    LedgerDb,
    _draft,
    ledger,
)

KEY = b"a-chain-key-from-a-kubernetes-secret"


async def _disable_triggers(db: LedgerDb) -> None:
    if db.backend == "postgres":
        await db.execute("ALTER TABLE savings_ledger_entries DISABLE TRIGGER trg_savings_ledger_entries_append_only")
    else:
        await db.execute("DROP TRIGGER trg_savings_ledger_entries_no_update")
        await db.execute("DROP TRIGGER trg_savings_ledger_entries_no_delete")


async def _rewrite(db: LedgerDb, entries: List[LedgerEntry], *, downgrade_from: int) -> None:
    """SIM4: x100 every value and re-chain with the only rule an attacker without the key can compute.

    Entries from ``downgrade_from`` on are also stripped of their key id (plain sha256 entries).
    """
    prev = None
    for entry in entries:
        key_id = None if entry.seq >= downgrade_from else entry.chain_key_id
        forged = replace(entry, value=entry.value * 100, prev_hash=prev, chain_key_id=key_id)
        forged = replace(forged, hash=chain_hash(prev, forged.canonical()))
        key_id = "NULL" if forged.chain_key_id is None else f"'{forged.chain_key_id}'"
        prev_sql = "NULL" if prev is None else f"'{prev}'"
        await db.execute(
            f"UPDATE savings_ledger_entries SET value = {forged.value}, prev_hash = {prev_sql}, "
            f"hash = '{forged.hash}', chain_key_id = {key_id} WHERE entry_id = '{entry.entry_id}'"
        )
        prev = forged.hash
    await db.repo.record_chain_status(
        ChainStatus(
            cluster_uid=CLUSTER_UID,
            verified_at=entries[-1].created_at,
            ok=True,
            entries=len(entries),
            head_seq=entries[-1].seq,
            head_hash=prev,
            failure=None,
        )
    )


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.parametrize(("downgrade_from", "failure_seq"), [(99, 1), (9, 1)], ids=["keep-key-id", "downgrade-tail"])
async def test_sim4_rewrite_of_a_keyed_chain_fails_with_hash_mismatch(
    ledger: LedgerDb,  # noqa: F811
    downgrade_from: int,
    failure_seq: int,
):
    repo = LedgerRepository(ledger.manager, chain_key=KEY)
    await repo.append(CLUSTER_UID, CLUSTER, [_draft(f"k{i}", 0.005) for i in range(20)])
    assert (await verify_ledger(repo, checkpoints={}))[CLUSTER_UID].ok
    entries = await repo.list_entries(CLUSTER_UID, limit=100, ascending=True)
    assert all(entry.chain_key_id == chain_key_id(KEY) for entry in entries)

    await _disable_triggers(ledger)
    await ledger.execute("DELETE FROM savings_ledger_entries WHERE seq > 16")
    await _rewrite(ledger, entries[:16], downgrade_from=downgrade_from)

    result = (await verify_ledger(repo, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.reason == "hash_mismatch" and result.failure_seq == failure_seq


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_keyed_chain_without_its_key_fails_verification(ledger: LedgerDb):  # noqa: F811
    await LedgerRepository(ledger.manager, chain_key=KEY).append(CLUSTER_UID, CLUSTER, [_draft("k0")])
    unkeyed = (await verify_ledger(LedgerRepository(ledger.manager, chain_key=b""), checkpoints={}))[CLUSTER_UID]
    assert not unkeyed.ok and unkeyed.reason == "chain_key_missing"
    other = LedgerRepository(ledger.manager, chain_key=b"another key")
    wrong = (await verify_ledger(other, checkpoints={}))[CLUSTER_UID]
    assert not wrong.ok and wrong.reason == "chain_key_mismatch"


@pytest.mark.asyncio
@pytest.mark.database
async def test_upgrade_keeps_entries_written_without_a_key_valid(ledger: LedgerDb):  # noqa: F811
    """Entries chained by the current release (sha256, no key) keep chain.ok once a key is configured."""
    before = LedgerRepository(ledger.manager, chain_key=b"")
    await before.append(CLUSTER_UID, CLUSTER, [_draft(f"old{i}") for i in range(3)])
    old = await before.list_entries(CLUSTER_UID, limit=10, ascending=True)
    assert all(entry.chain_key_id is None for entry in old)
    # Hash rule of the release before LED-05: sha256 over a payload without chain_key_id.
    assert all("chain_key_id" not in entry.payload() for entry in old)
    assert all("annual_rate" not in entry.payload() for entry in old)
    assert (await verify_ledger(before, checkpoints={}))[CLUSTER_UID].ok

    after = LedgerRepository(ledger.manager, chain_key=KEY)
    await after.append(CLUSTER_UID, CLUSTER, [_draft(f"new{i}") for i in range(3)])
    result = (await verify_ledger(after, checkpoints={}, full=True))[CLUSTER_UID]
    assert result.ok and result.entries == 6
    entries = await after.list_entries(CLUSTER_UID, limit=10, ascending=True)
    assert [entry.chain_key_id is not None for entry in entries] == [False] * 3 + [True] * 3
    assert math.isclose(sum(entry.value for entry in entries), 6.0)


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_unkeyed_entry_after_a_keyed_one_fails(ledger: LedgerDb):  # noqa: F811
    keyed = LedgerRepository(ledger.manager, chain_key=KEY)
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft("k0")])
    # The writer refuses (fail closed), so the forged unkeyed entry is inserted directly.
    from greenkube.verify.ledger.model import place

    head = (await keyed.list_entries(CLUSTER_UID, limit=1))[0]
    forged = place(
        _draft("k1"),
        cluster_uid=CLUSTER_UID,
        cluster_name=CLUSTER,
        seq=2,
        prev_hash=head.hash,
        created_at=head.created_at,
    )
    async with ledger.manager.connection_scope() as conn:
        columns = ", ".join(keyed._column_names())
        marks = ", ".join("?" for _ in keyed._column_names())
        await keyed._execute(
            conn, f"INSERT INTO savings_ledger_entries ({columns}) VALUES ({marks})", keyed._params(forged)
        )
        if ledger.backend == "sqlite":
            await conn.commit()
    result = (await verify_ledger(keyed, checkpoints={}))[CLUSTER_UID]
    assert not result.ok and result.reason == "hash_mismatch" and result.failure_seq == 2


@pytest.mark.asyncio
@pytest.mark.database
async def test_keyed_head_is_published(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import CHAIN_HEAD_KEYED

    repo = LedgerRepository(ledger.manager, chain_key=KEY)
    await LedgerRepository(ledger.manager, chain_key=b"").append(CLUSTER_UID, CLUSTER, [_draft("k0")])
    await verify_ledger(repo, checkpoints={})
    assert CHAIN_HEAD_KEYED.labels(cluster_uid=CLUSTER_UID)._value.get() == 0
    await repo.append(CLUSTER_UID, CLUSTER, [_draft("k1")])
    checkpoints: dict = {}
    await verify_ledger(repo, checkpoints=checkpoints)
    assert CHAIN_HEAD_KEYED.labels(cluster_uid=CLUSTER_UID)._value.get() == 1
    # An incremental pass with nothing new keeps the keyed state of its anchor.
    await verify_ledger(repo, checkpoints=checkpoints)
    assert CHAIN_HEAD_KEYED.labels(cluster_uid=CLUSTER_UID)._value.get() == 1


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_operator_accepts_the_head_after_a_restore_lost_the_tail(ledger: LedgerDb):  # noqa: F811
    """LED-05: an explicit, logged acceptance replaces deleting the status row; a forged chain is refused."""
    from greenkube.verify.ledger.verification import accept_head

    repo = LedgerRepository(ledger.manager, chain_key=KEY)
    await repo.append(CLUSTER_UID, CLUSTER, [_draft(f"k{i}") for i in range(5)])
    assert (await verify_ledger(repo, checkpoints={}))[CLUSTER_UID].ok
    await _disable_triggers(ledger)
    # A restore brings the entries and their totals back together (they commit in one transaction).
    await ledger.execute("DELETE FROM savings_ledger_entries WHERE seq = 5")
    await ledger.execute(
        "UPDATE savings_ledger_totals SET value = value - 1, positive = positive - 1, entries = entries - 1"
    )
    assert (await verify_ledger(repo, checkpoints={}))[CLUSTER_UID].reason == "truncated"

    with pytest.raises(ValueError):
        await accept_head(repo, CLUSTER_UID, "  ")
    accepted = await accept_head(repo, CLUSTER_UID, "restored from the backup of 03:00")
    assert accepted.ok and accepted.head_seq == 4
    assert (await verify_ledger(repo, checkpoints={}))[CLUSTER_UID].ok

    await ledger.execute("UPDATE savings_ledger_entries SET value = value * 2 WHERE seq = 2")
    refused = await accept_head(repo, CLUSTER_UID, "should not pass")
    assert not refused.ok and refused.reason == "hash_mismatch"
    status = await repo.get_chain_status(CLUSTER_UID)
    assert status is not None and status.head_seq == 4


@pytest.mark.asyncio
@pytest.mark.database
async def test_entries_of_every_writer_version_verify(ledger: LedgerDb):  # noqa: F811
    """ledger/3 (IMP-06) and ledger/4 (LED-11) entries share one chain; the version is only a label."""
    from greenkube.verify.ledger.model import LEDGER_WRITER_VERSION

    assert LEDGER_WRITER_VERSION == "ledger/4"
    drafts = [replace(_draft(f"v{version}"), method_version=f"ledger/{version}:projected") for version in (1, 2, 3, 4)]
    await LedgerRepository(ledger.manager, chain_key=KEY).append(CLUSTER_UID, CLUSTER, drafts)
    result = (await verify_ledger(LedgerRepository(ledger.manager, chain_key=KEY), checkpoints={}, full=True))[
        CLUSTER_UID
    ]
    assert result.ok and result.entries == 4


def _chain_key_errors() -> float:
    from greenkube.verify.ledger.projector import LEDGER_WRITE_ERRORS

    return LEDGER_WRITE_ERRORS.labels(reason="chain_key")._value.get()


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.parametrize("current", [b"", b"a-regenerated-secret"], ids=["missing", "mismatched"])
async def test_the_writer_fails_closed_after_keyed_entries_without_their_key(ledger: LedgerDb, current: bytes):  # noqa: F811
    """A lost or regenerated Secret never appends unkeyed or differently keyed entries after keyed ones."""
    from greenkube.storage.ledger_repository import LedgerChainKeyError

    await LedgerRepository(ledger.manager, chain_key=KEY).append(CLUSTER_UID, CLUSTER, [_draft("k0")])
    before = _chain_key_errors()
    with pytest.raises(LedgerChainKeyError):
        await LedgerRepository(ledger.manager, chain_key=current).append(CLUSTER_UID, CLUSTER, [_draft("k1")])
    assert _chain_key_errors() == before + 1
    assert await ledger.repo.count(CLUSTER_UID) == 1
    assert (await verify_ledger(LedgerRepository(ledger.manager, chain_key=KEY), checkpoints={}))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_deliberate_rotation_keeps_the_previous_key_for_verification(ledger: LedgerDb):  # noqa: F811
    new = b"the-rotated-chain-key"
    await LedgerRepository(ledger.manager, chain_key=KEY).append(CLUSTER_UID, CLUSTER, [_draft("k0")])
    rotated = LedgerRepository(ledger.manager, chain_key=new, previous_keys=[KEY])
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft("k1")])
    entries = await rotated.list_entries(CLUSTER_UID, limit=10, ascending=True)
    assert [entry.chain_key_id for entry in entries] == [chain_key_id(KEY), chain_key_id(new)]
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok
    without_previous = LedgerRepository(ledger.manager, chain_key=new)
    result = (await verify_ledger(without_previous, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.reason == "chain_key_mismatch"
