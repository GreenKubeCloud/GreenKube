"""Sequence-based key boundaries of the savings ledger chain (LED-05), both backends.

The HMAC-signed status row records ``first_keyed_seq`` (the upgrade) and the
seq at which each key took over (``greenkube db ledger-rotate-key``). No rule
depends on time.
"""

from __future__ import annotations

from dataclasses import replace
from typing import List, Optional

import pytest

from greenkube.storage.ledger_repository import ChainStatus, LedgerChainKeyError, LedgerRepository
from greenkube.verify.ledger.model import LedgerEntry, chain_hash, chain_key_id, keyed_chain_hash
from greenkube.verify.ledger.verification import rotate_key, verify_ledger
from tests.storage.test_ledger_chain_key import _disable_triggers  # pyrefly: ignore[missing-import]
from tests.storage.test_ledger_v2_contract import (  # pyrefly: ignore[missing-import]  # noqa: F401
    CLUSTER,
    CLUSTER_UID,
    LedgerDb,
    _draft,
    _hour,
    ledger,
)

K1 = b"the-leaked-first-key"
K2 = b"the-rotated-second-key"
K = b"the-chain-key"


async def _rechain(db: LedgerDb, entries: List[LedgerEntry], keys: List[Optional[bytes]], prev: Optional[str]) -> None:
    """Re-chain ``entries`` in place (x100 values), each with its key (HMAC) or none (sha256), after ``prev``."""
    for entry, key in zip(entries, keys):
        forged = replace(
            entry, value=entry.value * 100, prev_hash=prev, chain_key_id=chain_key_id(key) if key else None
        )
        canonical = forged.canonical()
        forged = replace(forged, hash=keyed_chain_hash(key, prev, canonical) if key else chain_hash(prev, canonical))
        key_sql = "NULL" if forged.chain_key_id is None else f"'{forged.chain_key_id}'"
        prev_sql = "NULL" if prev is None else f"'{prev}'"
        await db.execute(
            f"UPDATE savings_ledger_entries SET value = {forged.value}, prev_hash = {prev_sql}, "
            f"hash = '{forged.hash}', chain_key_id = {key_sql} WHERE entry_id = '{entry.entry_id}'"
        )
        prev = forged.hash


async def _rotated_chain(db: LedgerDb) -> LedgerRepository:
    """Three K1 entries, a recorded rotation to K2, then three K2 entries."""
    old = _hour(-24 * 30)
    first = LedgerRepository(db.manager, chain_key=K1)
    await first.append(
        CLUSTER_UID, CLUSTER, [replace(_draft(f"a{i}"), period_start=old, period_end=old) for i in range(3)], now=old
    )
    rotated = LedgerRepository(db.manager, chain_key=K2, previous_keys=[K1])
    assert (await rotate_key(rotated, CLUSTER_UID, "K1 leaked")).ok
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft(f"b{i}") for i in range(3)])
    return rotated


@pytest.mark.asyncio
@pytest.mark.database
async def test_the_upgrade_with_future_unkeyed_periods_verifies(ledger: LedgerDb):  # noqa: F811
    legacy = LedgerRepository(ledger.manager, chain_key=b"")
    future = _hour(24 * 20)  # LED-06's forward clock wrote future periods
    await legacy.append(
        CLUSTER_UID, CLUSTER, [replace(_draft(f"f{i}"), period_start=future, period_end=future) for i in range(2)]
    )
    await verify_ledger(legacy, checkpoints={})
    keyed = LedgerRepository(ledger.manager, chain_key=K)
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft("k0"), _draft("k1")])
    status = await keyed.get_chain_status(CLUSTER_UID)
    assert status is not None and status.first_keyed_seq == 3
    assert status.key_boundaries == ((chain_key_id(K), 3),)
    assert keyed.status_signature_state(status) == "current"
    assert (await verify_ledger(keyed, checkpoints={}, full=True))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_rotation_through_the_command_verifies_after_a_long_idle_period(ledger: LedgerDb):  # noqa: F811
    rotated = await _rotated_chain(ledger)
    status = await rotated.get_chain_status(CLUSTER_UID)
    assert status is not None and status.key_boundaries == ((chain_key_id(K1), 1), (chain_key_id(K2), 4))
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok
    audits = await rotated.head_acceptances(CLUSTER_UID)
    assert audits and audits[-1]["reason"].startswith("rotate-key to ")


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_leaked_old_key_cannot_rekey_entries_after_the_boundary(ledger: LedgerDb):  # noqa: F811
    rotated = await _rotated_chain(ledger)
    entries = await rotated.list_entries(CLUSTER_UID, limit=10, ascending=True)
    await _disable_triggers(ledger)
    await _rechain(ledger, entries[3:], [K1, K1, K1], entries[2].hash)
    result = (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.reason == "chain_key_order" and result.failure_seq == 4


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_status_row_signed_with_the_old_key_after_the_rotation_fails(ledger: LedgerDb):  # noqa: F811
    rotated = await _rotated_chain(ledger)
    entries = await rotated.list_entries(CLUSTER_UID, limit=10, ascending=True)
    await _disable_triggers(ledger)
    # The attacker re-keys everything with K1 and signs a status row with K1 (boundary on K1 only).
    await _rechain(ledger, entries, [K1] * len(entries), None)
    attacker = LedgerRepository(ledger.manager, chain_key=K1)
    head = (await attacker.list_entries(CLUSTER_UID, limit=1))[0]
    await attacker.record_chain_status(
        ChainStatus(
            cluster_uid=CLUSTER_UID,
            verified_at=head.created_at,
            ok=True,
            entries=len(entries),
            head_seq=head.seq,
            head_hash=head.hash,
            failure=None,
            head_keyed=True,
            first_keyed_seq=1,
            key_boundaries=((chain_key_id(K1), 1),),
        )
    )
    result = (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.reason == "status_forged"


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_append_after_an_unrecorded_rotation_fails_closed_until_the_command(ledger: LedgerDb):  # noqa: F811
    await LedgerRepository(ledger.manager, chain_key=K1).append(CLUSTER_UID, CLUSTER, [_draft("a0")])
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    with pytest.raises(LedgerChainKeyError, match="ledger-rotate-key"):
        await rotated.append(CLUSTER_UID, CLUSTER, [_draft("b0")])
    assert await rotated.count(CLUSTER_UID) == 1
    assert (await rotate_key(rotated, CLUSTER_UID, "planned rotation")).ok
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft("b0")])
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_unkeyed_entry_above_the_first_keyed_seq_fails(ledger: LedgerDb):  # noqa: F811
    keyed = LedgerRepository(ledger.manager, chain_key=K)
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft(f"k{i}") for i in range(3)])
    entries = await keyed.list_entries(CLUSTER_UID, limit=10, ascending=True)
    await _disable_triggers(ledger)
    # Backdating does not help: the rule is by seq, and the chain links every entry.
    old = _hour(-24 * 365)
    backdated = replace(entries[2], period_start=old, period_end=old, created_at=old)
    await _rechain(ledger, [backdated], [None], entries[1].hash)
    result = (await verify_ledger(keyed, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.failure_seq == 3


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_older_key_after_a_newer_one_fails(ledger: LedgerDb):  # noqa: F811
    rotated = await _rotated_chain(ledger)
    entries = await rotated.list_entries(CLUSTER_UID, limit=10, ascending=True)
    await _disable_triggers(ledger)
    await _rechain(ledger, entries[4:5], [K1], entries[3].hash)
    result = (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.reason == "chain_key_order" and result.failure_seq == 5


@pytest.mark.asyncio
@pytest.mark.database
async def test_rotating_twice_to_the_same_key_is_refused(ledger: LedgerDb):  # noqa: F811
    rotated = await _rotated_chain(ledger)
    with pytest.raises(ValueError):
        await rotate_key(rotated, CLUSTER_UID, "again")


async def _snapshot(db: LedgerDb, table: str) -> list:
    async with db.manager.connection_scope() as conn:
        return await db.repo._fetch(conn, f"SELECT * FROM {table}")


async def _restore(db: LedgerDb, table: str, rows: list) -> None:
    await db.execute(f"DELETE FROM {table}")
    async with db.manager.transaction() as conn:
        for row in rows:
            columns = list(row)
            marks = ", ".join("?" for _ in columns)
            await db.repo._execute(
                conn, f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", [row[c] for c in columns]
            )


async def _insert_forged(db: LedgerDb, repo: LedgerRepository, key: bytes, names: List[str]) -> None:
    """Append entries chained with ``key`` behind the writer's back (what an unguarded old pod would write)."""
    from greenkube.verify.ledger.model import place

    head = (await repo.list_entries(CLUSTER_UID, limit=1))[0]
    prev, seq = head.hash, head.seq
    async with db.manager.transaction() as conn:
        for name in names:
            seq += 1
            entry = place(
                _draft(name),
                cluster_uid=CLUSTER_UID,
                cluster_name=CLUSTER,
                seq=seq,
                prev_hash=prev,
                created_at=head.created_at,
                chain_key=key,
            )
            columns = repo._column_names()
            await repo._execute(
                conn,
                f"INSERT INTO savings_ledger_entries ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                repo._params(entry),
            )
            await repo._add_totals(conn, CLUSTER_UID, [entry])
            prev = entry.hash


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_old_key_writer_after_a_recorded_rotation_is_refused(ledger: LedgerDb):  # noqa: F811
    rotated = await _rotated_chain(ledger)
    before = await rotated.count(CLUSTER_UID)
    with pytest.raises(LedgerChainKeyError, match="ledger-rotate-key"):
        await LedgerRepository(ledger.manager, chain_key=K1).append(CLUSTER_UID, CLUSTER, [_draft("old-pod")])
    assert await rotated.count(CLUSTER_UID) == before


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_old_pod_during_the_rotation_window_is_refused(ledger: LedgerDb):  # noqa: F811
    """Rotation recorded before the old pods stopped: the K1 head equals the old pod's key, still refused."""
    await LedgerRepository(ledger.manager, chain_key=K1).append(CLUSTER_UID, CLUSTER, [_draft("a0")])
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    assert (await rotate_key(rotated, CLUSTER_UID, "rotation")).ok
    with pytest.raises(LedgerChainKeyError):
        await LedgerRepository(ledger.manager, chain_key=K1).append(CLUSTER_UID, CLUSTER, [_draft("old-pod")])
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft("b0")])
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_append_over_an_unkeyed_head_under_recorded_boundaries_is_refused(ledger: LedgerDb):  # noqa: F811
    keyed = LedgerRepository(ledger.manager, chain_key=K)
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft(f"k{i}") for i in range(3)])
    entries = await keyed.list_entries(CLUSTER_UID, limit=10, ascending=True)
    status = await keyed.get_chain_status(CLUSTER_UID)
    await _disable_triggers(ledger)
    await _rechain(ledger, entries, [None, None, None], None)  # a keyless rewrite, status row kept
    with pytest.raises(LedgerChainKeyError, match="unkeyed"):
        await keyed.append(CLUSTER_UID, CLUSTER, [_draft("k3")])
    after = await keyed.get_chain_status(CLUSTER_UID)
    assert after is not None and status is not None and after.key_boundaries == status.key_boundaries
    assert after.signature == status.signature


@pytest.mark.asyncio
@pytest.mark.database
async def test_accept_head_after_a_rotation_keeps_the_boundaries(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    rotated = await _rotated_chain(ledger)
    before = await rotated.get_chain_status(CLUSTER_UID)
    assert (await accept_head(rotated, CLUSTER_UID, "restore drill")).ok
    after = await rotated.get_chain_status(CLUSTER_UID)
    assert before is not None and after is not None and after.key_boundaries == before.key_boundaries
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_accept_head_rebuilds_boundaries_after_an_old_writer_broke_the_order(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    await LedgerRepository(ledger.manager, chain_key=K1).append(CLUSTER_UID, CLUSTER, [_draft("a0")])
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    assert (await rotate_key(rotated, CLUSTER_UID, "rotation")).ok
    # An unguarded old pod (the earlier release) appended K1 entries after the K2 boundary.
    await _insert_forged(ledger, rotated, K1, ["late-a", "late-b"])
    broken = (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID]
    assert not broken.ok and broken.reason == "chain_key_order"

    assert (await accept_head(rotated, CLUSTER_UID, "old pod wrote after the rotation", rebuild_boundaries=True)).ok
    audit = (await rotated.head_acceptances(CLUSTER_UID))[-1]
    # The K2 boundary moves past the old writer's entries; it is never removed.
    assert audit["key_boundaries"] == [[chain_key_id(K1), 1], [chain_key_id(K2), 4]]
    assert "rebuilt" in audit["reason"]
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft("b0")])
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_accept_head_refuses_an_entry_no_known_key_verifies(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    keyed = LedgerRepository(ledger.manager, chain_key=K1)
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft("a0")])
    await _insert_forged(ledger, keyed, b"an-unknown-key", ["x"])
    refused = await accept_head(keyed, CLUSTER_UID, "should not pass")
    assert not refused.ok and refused.reason == "chain_key_mismatch"


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_verifier_with_only_the_old_key_leaves_the_rotated_row_intact(ledger: LedgerDb):  # noqa: F811
    rotated = await _rotated_chain(ledger)
    before = await rotated.get_chain_status(CLUSTER_UID)
    old = LedgerRepository(ledger.manager, chain_key=K1)
    assert not (await verify_ledger(old, checkpoints={}, full=True))[CLUSTER_UID].ok
    after = await rotated.get_chain_status(CLUSTER_UID)
    assert before is not None and after is not None
    assert (after.signature, after.key_boundaries, after.head_seq, after.first_keyed_seq) == (
        before.signature,
        before.key_boundaries,
        before.head_seq,
        before.first_keyed_seq,
    )
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_restoring_a_pre_rotation_backup_then_rotating_works(ledger: LedgerDb):  # noqa: F811
    first = LedgerRepository(ledger.manager, chain_key=K1)
    await first.append(CLUSTER_UID, CLUSTER, [_draft(f"a{i}") for i in range(3)])
    assert (await verify_ledger(first, checkpoints={}))[CLUSTER_UID].ok
    backup = {table: await _snapshot(ledger, table) for table in ("ledger_chain_status", "savings_ledger_totals")}
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    assert (await rotate_key(rotated, CLUSTER_UID, "rotation")).ok
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft("b0")])
    # Restore the backup taken before the rotation.
    await _disable_triggers(ledger)
    await ledger.execute("DELETE FROM savings_ledger_entries WHERE seq > 3")
    for table, rows in backup.items():
        await _restore(ledger, table, rows)
    assert (await rotate_key(rotated, CLUSTER_UID, "rotation after the restore")).ok
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft("b0-again")])
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_rotate_key_writes_the_status_and_its_audit_together(ledger: LedgerDb):  # noqa: F811
    await LedgerRepository(ledger.manager, chain_key=K1).append(CLUSTER_UID, CLUSTER, [_draft("a0")])
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    before = await rotated.get_chain_status(CLUSTER_UID)
    original = rotated._execute

    async def failing(conn, query, params=()):
        if "INSERT INTO ledger_head_acceptances" in query:
            raise RuntimeError("crash between the two writes")
        return await original(conn, query, params)

    rotated._execute = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await rotate_key(rotated, CLUSTER_UID, "rotation")
    rotated._execute = original  # type: ignore[method-assign]
    after = await rotated.get_chain_status(CLUSTER_UID)
    assert before is not None and after is not None and after.signature == before.signature
    assert after.key_boundaries == before.key_boundaries
    assert await rotated.head_acceptances(CLUSTER_UID) == []


async def _keyless_rewrite_with_fixed_totals(db: LedgerDb, repo: LedgerRepository) -> None:
    """Forgery (a): every entry re-chained without the key, values unchanged so the totals still match."""
    entries = await repo.list_entries(CLUSTER_UID, limit=100, ascending=True)
    await _disable_triggers(db)
    prev = None
    for entry in entries:
        forged = replace(entry, prev_hash=prev, chain_key_id=None)
        forged = replace(forged, hash=chain_hash(prev, forged.canonical()))
        prev_sql = "NULL" if prev is None else f"'{prev}'"
        await db.execute(
            f"UPDATE savings_ledger_entries SET prev_hash = {prev_sql}, hash = '{forged.hash}', chain_key_id = NULL "
            f"WHERE entry_id = '{entry.entry_id}'"
        )
        prev = forged.hash


async def _rekey_tail_with(db: LedgerDb, repo: LedgerRepository, key: bytes, from_index: int) -> None:
    """Forgery (b): entries from ``from_index`` on re-chained with ``key`` (values unchanged)."""
    entries = await repo.list_entries(CLUSTER_UID, limit=100, ascending=True)
    await _disable_triggers(db)
    prev = entries[from_index - 1].hash
    for entry in entries[from_index:]:
        forged = replace(entry, prev_hash=prev, chain_key_id=chain_key_id(key))
        forged = replace(forged, hash=keyed_chain_hash(key, prev, forged.canonical()))
        await db.execute(
            f"UPDATE savings_ledger_entries SET prev_hash = '{prev}', hash = '{forged.hash}', "
            f"chain_key_id = '{forged.chain_key_id}' WHERE entry_id = '{entry.entry_id}'"
        )
        prev = forged.hash


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.parametrize("rebuild", [False, True], ids=["plain", "rebuild-flag"])
async def test_accept_head_refuses_a_keyless_rewrite(ledger: LedgerDb, rebuild: bool):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    rotated = await _rotated_chain(ledger)
    before = await rotated.get_chain_status(CLUSTER_UID)
    await _keyless_rewrite_with_fixed_totals(ledger, rotated)
    result = await accept_head(rotated, CLUSTER_UID, "forgery a", rebuild_boundaries=rebuild)
    assert not result.ok
    after = await rotated.get_chain_status(CLUSTER_UID)
    assert after is not None and before is not None and after.key_boundaries == before.key_boundaries
    with pytest.raises(LedgerChainKeyError):
        await rotated.append(CLUSTER_UID, CLUSTER, [_draft("re-anchor")])


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.parametrize("rebuild", [False, True], ids=["plain", "rebuild-flag"])
async def test_accept_head_refuses_a_leaked_old_key_after_the_boundary(ledger: LedgerDb, rebuild: bool):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    rotated = await _rotated_chain(ledger)  # K1 at 1-3, K2 from 4 (entries 4-6)
    await _rekey_tail_with(ledger, rotated, K1, 5)  # seq 6 re-keyed with K1: K1, K2, K1
    result = await accept_head(rotated, CLUSTER_UID, "forgery b", rebuild_boundaries=rebuild)
    assert not result.ok
    status = await rotated.get_chain_status(CLUSTER_UID)
    assert status is not None and status.key_boundaries == ((chain_key_id(K1), 1), (chain_key_id(K2), 4))


@pytest.mark.asyncio
@pytest.mark.database
async def test_the_old_writer_extension_is_rebuilt_only_with_the_flag(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    await LedgerRepository(ledger.manager, chain_key=K1).append(CLUSTER_UID, CLUSTER, [_draft("a0")])
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    assert (await rotate_key(rotated, CLUSTER_UID, "rotation")).ok  # K2 from seq 2
    await _insert_forged(ledger, rotated, K1, ["late-a", "late-b"])  # an old writer extends K1 to seq 3
    refused = await accept_head(rotated, CLUSTER_UID, "without the flag")
    assert not refused.ok and "--rebuild-boundaries" in (refused.detail or "")

    assert (await accept_head(rotated, CLUSTER_UID, "old writer", rebuild_boundaries=True)).ok
    status = await rotated.get_chain_status(CLUSTER_UID)
    assert status is not None and status.key_boundaries == ((chain_key_id(K1), 1), (chain_key_id(K2), 4))
    audit = (await rotated.head_acceptances(CLUSTER_UID))[-1]
    assert "rebuilt" in audit["reason"] and audit["key_boundaries"] == [[chain_key_id(K1), 1], [chain_key_id(K2), 4]]
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft("b0")])
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_rebuild_without_a_trusted_row_is_refused(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    keyed = LedgerRepository(ledger.manager, chain_key=K1)
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft("a0"), _draft("a1")])
    await ledger.execute("DELETE FROM ledger_chain_status")
    with pytest.raises(ValueError, match="trusted"):
        await accept_head(keyed, CLUSTER_UID, "no row", rebuild_boundaries=True)
    refused = await accept_head(keyed, CLUSTER_UID, "no row")
    assert not refused.ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_verifier_that_read_the_row_before_an_accept_does_not_overwrite_it(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    await LedgerRepository(ledger.manager, chain_key=K1).append(CLUSTER_UID, CLUSTER, [_draft("a0")])
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    assert (await rotate_key(rotated, CLUSTER_UID, "rotation")).ok
    stale = await rotated.get_chain_status(CLUSTER_UID)
    await _insert_forged(ledger, rotated, K1, ["late-a", "late-b"])
    assert (await accept_head(rotated, CLUSTER_UID, "old writer", rebuild_boundaries=True)).ok
    rotated_row = await rotated.get_chain_status(CLUSTER_UID)

    async def stale_read(cluster_uid):  # the verifier read the row before the rotation committed
        return stale

    verifier = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    verifier.get_chain_status = stale_read  # type: ignore[method-assign]
    await verify_ledger(verifier, checkpoints={}, full=True)
    after = await rotated.get_chain_status(CLUSTER_UID)
    assert after is not None and rotated_row is not None
    assert (after.signature, after.key_boundaries) == (rotated_row.signature, rotated_row.key_boundaries)


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_rebuild_never_moves_a_boundary_the_anchor_already_verified(ledger: LedgerDb):  # noqa: F811
    """Leaked K1 re-keys every K2 entry (4-6) after the anchor at 6 verified them under K2: refused."""
    from greenkube.verify.ledger.verification import accept_head

    rotated = await _rotated_chain(ledger)
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok  # anchor at seq 6
    await _rekey_tail_with(ledger, rotated, K1, 3)  # seq 4-6 re-keyed with K1, totals unchanged
    result = await accept_head(rotated, CLUSTER_UID, "leaked K1", rebuild_boundaries=True)
    assert not result.ok and result.reason == "boundary_rebuild_refused"
    status = await rotated.get_chain_status(CLUSTER_UID)
    assert status is not None and status.key_boundaries == ((chain_key_id(K1), 1), (chain_key_id(K2), 4))


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_deleted_status_row_on_a_keyed_chain_is_recovered_by_trusting_entry_keys(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    rotated = await _rotated_chain(ledger)
    await ledger.execute("DELETE FROM ledger_chain_status")
    assert not (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok
    assert not (await accept_head(rotated, CLUSTER_UID, "lost row")).ok
    assert (await accept_head(rotated, CLUSTER_UID, "lost row", trust_entry_keys=True)).ok
    status = await rotated.get_chain_status(CLUSTER_UID)
    assert status is not None and status.key_boundaries == ((chain_key_id(K1), 1), (chain_key_id(K2), 4))
    assert status.first_keyed_seq == 1
    audit = (await rotated.head_acceptances(CLUSTER_UID))[-1]
    assert "trust-entry-keys" in audit["reason"]
    assert audit["key_boundaries"] == [[chain_key_id(K1), 1], [chain_key_id(K2), 4]]
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft("after-recovery")])
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_trusting_entry_keys_is_refused_when_a_trusted_row_exists(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    rotated = await _rotated_chain(ledger)
    with pytest.raises(ValueError, match="trusted"):
        await accept_head(rotated, CLUSTER_UID, "not needed", trust_entry_keys=True)


@pytest.mark.asyncio
@pytest.mark.database
async def test_trusting_entry_keys_refuses_an_old_key_after_a_newer_one(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    rotated = await _rotated_chain(ledger)
    await _rekey_tail_with(ledger, rotated, K1, 5)  # K1, K2, K1
    await ledger.execute("DELETE FROM ledger_chain_status")
    result = await accept_head(rotated, CLUSTER_UID, "lost row", trust_entry_keys=True)
    assert not result.ok and result.reason == "boundary_rebuild_refused"


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_stale_verifier_does_not_flip_the_outcome_of_a_row_just_accepted(ledger: LedgerDb):  # noqa: F811
    rotated = await _rotated_chain(ledger)
    row = await rotated.get_chain_status(CLUSTER_UID)
    assert row is not None
    await rotated.record_verification_result(
        CLUSTER_UID, verified_at=row.verified_at, ok=False, failure="stale", expect_signature="not-the-signature"
    )
    after = await rotated.get_chain_status(CLUSTER_UID)
    assert after is not None and after.ok and after.failure is None
