"""Key order after a rotation and the external key anchor LEDGER_CHAIN_KEY_SINCE (LED-05), both backends."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import List

import pytest

from greenkube.storage.ledger_repository import LedgerRepository
from greenkube.verify.ledger.model import LedgerEntry, chain_hash, chain_key_id, keyed_chain_hash
from greenkube.verify.ledger.verification import verify_ledger
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


async def _rechain(db: LedgerDb, entries: List[LedgerEntry], key: bytes | None) -> None:
    """Re-chain ``entries`` (x100 values) with ``key`` (HMAC) or without a key (sha256), in place."""
    prev = None
    for entry in entries:
        forged = replace(
            entry, value=entry.value * 100, prev_hash=prev, chain_key_id=chain_key_id(key) if key else None
        )
        canonical = forged.canonical()
        forged = replace(forged, hash=keyed_chain_hash(key, prev, canonical) if key else chain_hash(prev, canonical))
        key_id = "NULL" if forged.chain_key_id is None else f"'{forged.chain_key_id}'"
        prev_sql = "NULL" if prev is None else f"'{prev}'"
        await db.execute(
            f"UPDATE savings_ledger_entries SET value = {forged.value}, prev_hash = {prev_sql}, "
            f"hash = '{forged.hash}', chain_key_id = {key_id} WHERE entry_id = '{entry.entry_id}'"
        )
        prev = forged.hash


async def _forge_status(db: LedgerDb, repo: LedgerRepository, entries: List[LedgerEntry], keyed: bool) -> None:
    """Replace the status row with one signed by ``repo``'s current key (the attacker's)."""
    head = (await repo.list_entries(CLUSTER_UID, limit=1))[0]
    await db.execute("DELETE FROM ledger_chain_status")
    from greenkube.storage.ledger_repository import ChainStatus

    await repo.record_chain_status(
        ChainStatus(
            cluster_uid=CLUSTER_UID,
            verified_at=head.created_at,
            ok=True,
            entries=len(entries),
            head_seq=head.seq,
            head_hash=head.hash,
            failure=None,
            head_keyed=keyed,
        )
    )


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_leaked_previous_key_cannot_rekey_the_chain_after_a_rotation(ledger: LedgerDb):  # noqa: F811
    first = LedgerRepository(ledger.manager, chain_key=K1)
    await first.append(CLUSTER_UID, CLUSTER, [_draft(f"a{i}") for i in range(3)])
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft(f"b{i}") for i in range(3)])
    assert (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID].ok

    # The attacker holds K1: re-key every entry with it and sign the status row with it.
    entries = await rotated.list_entries(CLUSTER_UID, limit=100, ascending=True)
    await _disable_triggers(ledger)
    await _rechain(ledger, entries, K1)
    await _forge_status(ledger, LedgerRepository(ledger.manager, chain_key=K1), entries, keyed=True)
    result = (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.reason == "status_forged"


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_older_key_after_a_newer_one_fails_the_key_order(ledger: LedgerDb):  # noqa: F811
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1])
    await LedgerRepository(ledger.manager, chain_key=K1).append(CLUSTER_UID, CLUSTER, [_draft("a0")])
    await rotated.append(CLUSTER_UID, CLUSTER, [_draft("b0")])
    entries = await rotated.list_entries(CLUSTER_UID, limit=10, ascending=True)
    await _disable_triggers(ledger)
    # K2 first, then the older K1: a leaked old key may not follow the newer one.
    prev = None
    for entry, key in zip(entries, (K2, K1)):
        forged = replace(entry, prev_hash=prev, chain_key_id=chain_key_id(key))
        forged = replace(forged, hash=keyed_chain_hash(key, prev, forged.canonical()))
        prev_sql = "NULL" if prev is None else f"'{prev}'"
        await ledger.execute(
            f"UPDATE savings_ledger_entries SET prev_hash = {prev_sql}, hash = '{forged.hash}', "
            f"chain_key_id = '{forged.chain_key_id}' WHERE entry_id = '{forged.entry_id}'"
        )
        prev = forged.hash
    result = (await verify_ledger(rotated, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.reason == "chain_key_order" and result.failure_seq == 2


@pytest.mark.asyncio
@pytest.mark.database
async def test_the_genuine_upgrade_within_the_grace_period_passes(ledger: LedgerDb):  # noqa: F811
    legacy = LedgerRepository(ledger.manager, chain_key=b"")
    await legacy.append(CLUSTER_UID, CLUSTER, [_draft(f"old{i}") for i in range(3)])  # periods end at the hour
    since = _hour(0)
    keyed = LedgerRepository(ledger.manager, chain_key=K, key_since=since)
    # No status row yet (an idle cluster): written and accepted in the same run, no false status_missing.
    result = (await verify_ledger(keyed, checkpoints={}, now=since + timedelta(minutes=30)))[CLUSTER_UID]
    assert result.ok
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft("new")])
    assert (await verify_ledger(keyed, checkpoints={}, full=True, now=since + timedelta(hours=5)))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_full_downgrade_after_the_grace_period_fails(ledger: LedgerDb):  # noqa: F811
    since = _hour(-10)
    keyed = LedgerRepository(ledger.manager, chain_key=K, key_since=since)
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft(f"k{i}") for i in range(4)])  # periods end at _hour(0)
    entries = await keyed.list_entries(CLUSTER_UID, limit=100, ascending=True)
    await _disable_triggers(ledger)
    await _rechain(ledger, entries, None)
    await ledger.execute("DELETE FROM ledger_chain_status")
    unsigned = LedgerRepository(ledger.manager, chain_key=b"")
    await _forge_status(ledger, unsigned, entries, keyed=False)
    result = (await verify_ledger(keyed, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.reason in {"unkeyed_after_key", "status_unsigned"}
    # The writer never signs that unsigned anchor outside the grace period.
    status = await keyed.get_chain_status(CLUSTER_UID)
    assert status is not None and status.signature is None


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_unkeyed_entry_after_the_key_since_fails(ledger: LedgerDb):  # noqa: F811
    since = _hour(-10)
    keyed = LedgerRepository(ledger.manager, chain_key=K, key_since=since)
    legacy = LedgerRepository(ledger.manager, chain_key=b"")
    await legacy.append(CLUSTER_UID, CLUSTER, [_draft("late-unkeyed")])  # period ends at _hour(0), after since + 2 h
    await verify_ledger(legacy, checkpoints={})
    result = (await verify_ledger(keyed, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.reason == "unkeyed_after_key"


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_backdated_downgraded_tail_is_caught_by_the_chain(ledger: LedgerDb):  # noqa: F811
    since = _hour(-10)
    keyed = LedgerRepository(ledger.manager, chain_key=K, key_since=since)
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft(f"k{i}") for i in range(3)])
    entries = await keyed.list_entries(CLUSTER_UID, limit=10, ascending=True)
    await _disable_triggers(ledger)
    # The attacker backdates the last entry before the key existed and strips its key.
    old = since - timedelta(days=2)
    last = replace(entries[-1], period_start=old, period_end=old + timedelta(hours=1), created_at=old)
    await _rechain(ledger, [last], None)
    result = (await verify_ledger(keyed, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.failure_seq == 3


@pytest.mark.asyncio
@pytest.mark.database
async def test_a_rotation_on_an_idle_cluster_still_appends_after_the_grace_period(ledger: LedgerDb):  # noqa: F811
    from greenkube.verify.ledger.verification import accept_head

    first = LedgerRepository(ledger.manager, chain_key=K1)
    old = _hour(-10)
    await first.append(CLUSTER_UID, CLUSTER, [replace(_draft("a0"), period_start=old, period_end=old)], now=old)
    assert (await verify_ledger(first, checkpoints={}, now=old))[CLUSTER_UID].ok
    # Rotated at -8 h, nothing appended until now (more than 2 h after SINCE, by the wall clock too).
    rotated = LedgerRepository(ledger.manager, chain_key=K2, previous_keys=[K1], key_since=_hour(-8))
    later = _hour(0)
    await rotated.append(CLUSTER_UID, CLUSTER, [replace(_draft("b0"), period_start=later, period_end=later)], now=later)
    assert await rotated.count(CLUSTER_UID) == 2
    result = (await verify_ledger(rotated, checkpoints={}, full=True, now=later))[CLUSTER_UID]
    if not result.ok:
        assert result.reason == "status_forged"  # the old-key status row: accepted explicitly
        assert (await accept_head(rotated, CLUSTER_UID, "rotation after an idle period", now=later)).ok
    assert (await verify_ledger(rotated, checkpoints={}, full=True, now=later))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_unkeyed_entries_with_future_periods_before_the_upgrade_stay_valid(ledger: LedgerDb):  # noqa: F811
    """LED-06's forward clock wrote future periods: below the signed first keyed seq they are accepted."""
    legacy = LedgerRepository(ledger.manager, chain_key=b"")
    future = _hour(24 * 20)
    await legacy.append(
        CLUSTER_UID, CLUSTER, [replace(_draft(f"f{i}"), period_start=future, period_end=future) for i in range(2)]
    )
    await verify_ledger(legacy, checkpoints={})
    keyed = LedgerRepository(ledger.manager, chain_key=K, key_since=_hour(0))
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft("k0"), _draft("k1")], now=_hour(0))
    status = await keyed.get_chain_status(CLUSTER_UID)
    assert status is not None and status.first_keyed_seq == 3
    assert (await verify_ledger(keyed, checkpoints={}, full=True, now=_hour(30)))[CLUSTER_UID].ok


@pytest.mark.asyncio
@pytest.mark.database
async def test_an_unkeyed_entry_after_the_first_keyed_seq_fails(ledger: LedgerDb):  # noqa: F811
    keyed = LedgerRepository(ledger.manager, chain_key=K, key_since=_hour(-10))
    await keyed.append(CLUSTER_UID, CLUSTER, [_draft("k0"), _draft("k1")], now=_hour(-10))
    entries = await keyed.list_entries(CLUSTER_UID, limit=10, ascending=True)
    await _disable_triggers(ledger)
    prev = entries[0].hash
    forged = replace(entries[1], prev_hash=prev, chain_key_id=None)
    forged = replace(forged, hash=chain_hash(prev, forged.canonical()))
    await ledger.execute(
        f"UPDATE savings_ledger_entries SET hash = '{forged.hash}', chain_key_id = NULL "
        f"WHERE entry_id = '{forged.entry_id}'"
    )
    result = (await verify_ledger(keyed, checkpoints={}, full=True))[CLUSTER_UID]
    assert not result.ok and result.failure_seq == 2
