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
