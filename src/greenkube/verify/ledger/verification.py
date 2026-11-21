# src/greenkube/verify/ledger/verification.py
"""Hash-chain verification of the savings ledger v2 (roadmap C6, N7).

Each cluster's chain is walked in ``seq`` order. An entry passes when:

* its ``seq`` is the next position (a removed row leaves a gap);
* its ``prev_hash`` is the previous entry's ``hash`` (a reordered or forged row
  breaks the link);
* its ``hash`` equals ``sha256(prev_hash || canonical_json(entry))``, or
  ``HMAC-SHA256(key, ...)`` for an entry chained with ``LEDGER_CHAIN_KEY`` (a
  modified column changes it; LED-05);
* once an entry was chained with the key, every later entry is too (an
  unkeyed entry after a keyed one is a forgery: ``hash_mismatch``); a keyed
  entry needs the key (``chain_key_missing``) and the same key
  (``chain_key_mismatch``);
* its ``entry_id`` derives from its cluster and ``entry_key``, and it
  belongs to the chain's cluster;
* on a full pass, ``savings_ledger_totals`` (what the summary and the
  report read) equals a fold of the entries (``totals_mismatch``; LED-09);
* a reversal cancels its target exactly (opposite value, same quantity and
  unit; ``reversal_mismatch``) and only once per book (``double_reversal``), drift
  reversals excepted (LED-05).

The verified head is published as ``greenkube_ledger_chain_head_seq`` and
``greenkube_ledger_chain_head_info`` (hash label), so Prometheus retention is a
witness outside the database. Without ``LEDGER_CHAIN_KEY`` the chain uses no
secret: anyone who can write to the database can recompute it. With the key,
only its holder can (see the methodology's limits).

The head verified last is stored in ``ledger_chain_status``: a chain that is
shorter than that head, or whose entry at that position has another hash,
lost its tail. Every failed verification increments
``greenkube_ledger_chain_verification_failures_total{reason}`` and sets
``greenkube_ledger_chain_ok{cluster_uid}`` to 0 until a verification passes
again (the chart's PrometheusRule alerts on the gauge, so a failure found by
the first pass of a pod alerts too, and stays alerting while the chain is
broken) and is reported by ``GET /api/v2/ledger/summary``.

The controller loop ``ledger-chain`` first catches the v2 ledger up with the
v1 ledger (see :mod:`greenkube.verify.ledger.projector`), then verifies every
cluster's chain: incrementally from the last verified head on each tick, and
the whole chain once per :data:`FULL_VERIFICATION_INTERVAL` and on start.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set, Tuple

from prometheus_client import Counter, Gauge

from greenkube.core.observability import REGISTRY as OBSERVABILITY_REGISTRY
from greenkube.storage.ledger_repository import CHAIN_KEY_GRACE, ChainStatus, LedgerRepository
from greenkube.utils.time import ensure_utc
from greenkube.verify.ledger.model import entry_id_for

logger = logging.getLogger(__name__)

PAGE_SIZE = 5000
FULL_VERIFICATION_INTERVAL = timedelta(hours=24)
DEFAULT_INTERVAL_SECONDS = 3600.0

CHAIN_VERIFICATION_FAILURES = Counter(
    "greenkube_ledger_chain_verification_failures_total",
    "Savings ledger hash-chain verifications that found a modified, missing or reordered entry, by reason.",
    ["reason"],
    registry=OBSERVABILITY_REGISTRY,
)
CHAIN_VERIFIED_ENTRIES = Gauge(
    "greenkube_ledger_chain_verified_entries",
    "Entries of the savings ledger hash chain verified by the last pass (all clusters).",
    registry=OBSERVABILITY_REGISTRY,
)
CHAIN_HEAD_SEQ = Gauge(
    "greenkube_ledger_chain_head_seq",
    "Position of the last verified entry of the cluster's savings ledger chain. Prometheus retention keeps "
    "an independent record of the head: a later value below an earlier one means the tail was removed.",
    ["cluster_uid"],
    registry=OBSERVABILITY_REGISTRY,
)
CHAIN_HEAD = Gauge(
    "greenkube_ledger_chain_head_info",
    "Hash of the last verified entry of the cluster's savings ledger chain (value 1; the hash is a label).",
    ["cluster_uid", "head_seq", "head_hash"],
    registry=OBSERVABILITY_REGISTRY,
)
CHAIN_HEAD_KEYED = Gauge(
    "greenkube_ledger_chain_head_keyed",
    "1 when the last verified entry of the cluster's chain is chained with LEDGER_CHAIN_KEY (HMAC), 0 otherwise. "
    "Prometheus retention keeps its history: a drop from 1 to 0 means the chain was re-written without the key.",
    ["cluster_uid"],
    registry=OBSERVABILITY_REGISTRY,
)
CHAIN_OK = Gauge(
    "greenkube_ledger_chain_ok",
    "1 when the last verification of the cluster's savings ledger hash chain passed, 0 when it failed.",
    ["cluster_uid"],
    registry=OBSERVABILITY_REGISTRY,
)

#: cluster uid -> labels of its published head info series (one series per cluster).
_HEAD_LABELS: Dict[str, Tuple[str, str, str]] = {}


def _publish_head(cluster_uid: str, head_seq: int, head_hash: Optional[str]) -> None:
    """Publish the verified head outside the database (LED-05): Prometheus keeps its history."""
    CHAIN_HEAD_SEQ.labels(cluster_uid=cluster_uid).set(head_seq)
    labels = (cluster_uid, str(head_seq), head_hash or "")
    previous = _HEAD_LABELS.get(cluster_uid)
    if previous is not None and previous != labels:
        try:
            CHAIN_HEAD.remove(*previous)
        except KeyError:
            pass
    CHAIN_HEAD.labels(*labels).set(1)
    _HEAD_LABELS[cluster_uid] = labels


#: cluster uid -> (verified head seq, head hash, time of the last full verification), per process.
_CHECKPOINTS: Dict[str, Tuple[int, Optional[str], datetime]] = {}


@dataclass(frozen=True)
class ChainVerification:
    cluster_uid: str
    ok: bool
    entries: int
    head_seq: Optional[int]
    head_hash: Optional[str]
    failure_seq: Optional[int] = None
    reason: Optional[str] = None
    detail: Optional[str] = None
    #: Whether the verified head is chained with the key (LED-05).
    head_keyed: bool = False
    #: Key id of the verified head (None when unkeyed).
    head_key_id: Optional[str] = None
    #: Seq of the first keyed entry walked (full passes), or None.
    first_keyed_seq: Optional[int] = None


async def verify_chain(
    repo: LedgerRepository,
    cluster_uid: str,
    *,
    after_seq: int = 0,
    after_hash: Optional[str] = None,
    page_size: int = PAGE_SIZE,
    first_keyed_seq: Optional[int] = None,
) -> ChainVerification:
    """Walk the chain of ``cluster_uid`` after position ``after_seq`` (whose hash is ``after_hash``)."""
    expected_seq = after_seq + 1
    prev_hash = after_hash
    verified = after_seq
    keys = repo.chain_keys
    #: Key rank: 0 for the current key, then previous keys from newest to oldest. A chain only moves to newer keys.
    rank = {key_id: index for index, key_id in enumerate(repo.chain_key_order)}
    #: Whether an entry walked was chained with the key: later entries must be too.
    keyed_seen = False
    newest_rank: Optional[int] = None
    head_key_id: Optional[str] = None
    if after_seq and after_hash is not None:
        anchor = await repo.chain_page(cluster_uid, after_seq - 1, 1)
        keyed_seen = bool(anchor) and anchor[0].chain_key_id is not None
        if keyed_seen:
            head_key_id = anchor[0].chain_key_id
            newest_rank = rank.get(head_key_id or "")
    since = repo.key_since
    cutoff = since + CHAIN_KEY_GRACE if since is not None else None
    first_keyed_walked: Optional[int] = None
    #: entry id -> (value, quantity, unit) of the entries walked, for the reversal checks.
    seen: Dict[str, Tuple[float, str, str]] = {}
    #: ids reversed (non-drift) by the entries walked.
    reversed_ids: Set[Tuple[str, str]] = set()
    while True:
        page = await repo.chain_page(cluster_uid, expected_seq - 1, page_size)
        if not page:
            break
        for entry in page:
            failure: Optional[Tuple[str, str]] = None
            if entry.seq != expected_seq:
                failure = ("missing_entry", f"expected seq {expected_seq}, found {entry.seq}")
            elif entry.prev_hash != prev_hash:
                failure = ("broken_link", f"seq {entry.seq} does not follow the previous entry's hash")
            elif entry.chain_key_id is None and keyed_seen:
                failure = ("hash_mismatch", f"seq {entry.seq} ({entry.entry_id}) drops the chain key")
            elif entry.chain_key_id is not None and not keys:
                failure = ("chain_key_missing", f"seq {entry.seq} is chained with a key and LEDGER_CHAIN_KEY is unset")
            elif entry.chain_key_id is not None and entry.chain_key_id not in keys:
                failure = ("chain_key_mismatch", f"seq {entry.seq} is chained with another key")
            elif (
                entry.chain_key_id is not None
                and newest_rank is not None
                and rank.get(entry.chain_key_id, len(rank)) > newest_rank
            ):
                failure = ("chain_key_order", f"seq {entry.seq} uses an older key after a newer one")
            elif entry.hash != entry.computed_hash(keys.get(entry.chain_key_id or "")):
                failure = ("hash_mismatch", f"seq {entry.seq} ({entry.entry_id}) was modified")
            elif (
                cutoff is not None
                and entry.chain_key_id is None
                and (first_keyed_seq is None or entry.seq >= first_keyed_seq)
                and _entry_time(entry) > cutoff
            ):
                failure = ("unkeyed_after_key", f"seq {entry.seq} is unkeyed and later than LEDGER_CHAIN_KEY_SINCE")
            elif (
                cutoff is not None
                and entry.chain_key_id is not None
                and rank.get(entry.chain_key_id) != 0
                and _entry_time(entry) > cutoff
            ):
                failure = ("chain_key_order", f"seq {entry.seq} uses a previous key after LEDGER_CHAIN_KEY_SINCE")
            elif entry.entry_id != entry_id_for(cluster_uid, entry.entry_key):
                failure = ("identity_mismatch", f"seq {entry.seq} has an entry id that does not match its key")
            elif entry.cluster_uid != cluster_uid:
                failure = ("identity_mismatch", f"seq {entry.seq} belongs to cluster {entry.cluster_uid}")
            else:
                failure = await _reversal_mismatch(repo, entry, seen, reversed_ids)
            if failure is not None:
                return ChainVerification(
                    cluster_uid,
                    False,
                    verified,
                    entry.seq,
                    entry.hash,
                    failure_seq=entry.seq,
                    reason=failure[0],
                    detail=failure[1],
                )
            prev_hash = entry.hash
            keyed_seen = keyed_seen or entry.chain_key_id is not None
            head_key_id = entry.chain_key_id
            if entry.chain_key_id is not None and first_keyed_walked is None:
                first_keyed_walked = entry.seq
            if entry.chain_key_id is not None:
                entry_rank = rank.get(entry.chain_key_id)
                if entry_rank is not None:
                    newest_rank = entry_rank if newest_rank is None else min(newest_rank, entry_rank)
            seen[entry.entry_id] = (entry.value, entry.quantity, entry.unit)
            verified = entry.seq
            expected_seq += 1
        if len(page) < page_size:
            break
    return ChainVerification(
        cluster_uid,
        True,
        verified,
        verified or None,
        prev_hash,
        head_keyed=keyed_seen,
        head_key_id=head_key_id,
        first_keyed_seq=first_keyed_walked,
    )


def _entry_time(entry) -> datetime:
    """The latest instant an entry claims: its period end or its creation, whichever is later."""
    return max(ensure_utc(entry.period_end), ensure_utc(entry.created_at))


async def _reversal_mismatch(
    repo: LedgerRepository, entry, seen: Dict[str, Tuple[float, str, str]], reversed_ids: Set[Tuple[str, str]]
) -> Optional[Tuple[str, str]]:
    """A reversal must cancel its target exactly: opposite value, same quantity and unit (LED-05).

    Drift reversals are left out: they cancel the measured accruals of a
    period and name only the first one.
    """
    if entry.entry_kind != "reversal" or not entry.reverses_entry_id or entry.reason == "drift":
        return None
    # An entry is cancelled once per book: a projection is cancelled in the accrued book by a supersession and
    # in the restatement book by its restatement, but a second non-drift reversal in the same book would count
    # the cancellation twice.
    key = (entry.reverses_entry_id, entry.book)
    if key in reversed_ids:
        return ("double_reversal", f"seq {entry.seq} reverses an entry already reversed in the {entry.book} book")
    if entry.reverses_entry_id not in seen and await repo.reversal_count(
        entry.cluster_uid, entry.reverses_entry_id, entry.book, entry.seq
    ):
        return ("double_reversal", f"seq {entry.seq} reverses an entry already reversed in the {entry.book} book")
    reversed_ids.add(key)
    target = seen.get(entry.reverses_entry_id)
    if target is None:
        stored = await repo.get_entry(entry.reverses_entry_id)
        if stored is None:
            return ("reversal_mismatch", f"seq {entry.seq} reverses an entry that does not exist")
        target = (stored.value, stored.quantity, stored.unit)
    value, quantity, unit = target
    if entry.value != -value or entry.quantity != quantity or entry.unit != unit:
        return ("reversal_mismatch", f"seq {entry.seq} does not cancel the entry it reverses")
    return None


def _status_failure(
    repo: LedgerRepository, result: ChainVerification, previous: Optional[ChainStatus], moment: datetime
) -> Optional[ChainVerification]:
    """With a chain key, the stored anchor must be signed and present (LED-05).

    A signed row whose HMAC does not match was edited (``status_forged``). A
    missing row fails once the chain exists (``status_missing``): a keyed
    chain gets a signed row with its first append, and the reset is
    ``greenkube db ledger-accept-head``. An unsigned row (stored before
    migration 0294 or without a key) is accepted only while the chain holds no
    keyed entry; it is signed by this verification.
    """
    if not repo.chain_key or not result.entries:
        return None
    cluster_uid = result.cluster_uid

    def failure(reason: str, detail: str) -> ChainVerification:
        return replace(result, ok=False, failure_seq=None, reason=reason, detail=detail)

    since = repo.key_since
    in_grace = repo.within_key_grace(moment)
    if previous is None:
        if in_grace:
            # An idle cluster before its first keyed append: the anchor is written (signed) by this run.
            return None
        return failure("status_missing", f"the ledger chain status of cluster {cluster_uid} is missing")
    state = repo.status_signature_state(previous)
    if state == "forged":
        return failure("status_forged", f"the ledger chain status of cluster {cluster_uid} fails its signature")
    if state == "previous" and not in_grace:
        # Only the current key's signature counts: the writer re-signs the row with it after a rotation (with its
        # first append), and a row signed with an older, possibly leaked key is a forgery outside the grace period.
        return failure("status_forged", f"the ledger chain status of cluster {cluster_uid} uses an old key")
    if state == "unsigned":
        if since is not None and not in_grace:
            return failure("status_unsigned", f"the ledger chain status of cluster {cluster_uid} is not signed")
        if since is None and (previous.head_keyed or result.head_keyed):
            return failure("status_unsigned", f"the ledger chain status of keyed cluster {cluster_uid} is not signed")
    return None


async def _tail_lost(
    repo: LedgerRepository, result: ChainVerification, previous: Optional[ChainStatus]
) -> Optional[ChainVerification]:
    """A chain shorter than the head verified before, or with another hash at that head, lost its tail.

    The anchor is the last head that passed: a failed verification keeps it
    (see :func:`verify_ledger`), so a lost tail keeps failing, even once new
    entries reuse its positions, until an operator resets the status row.
    """
    cluster_uid = result.cluster_uid
    if previous is None or previous.head_seq is None:
        return None
    if (result.head_seq or 0) < previous.head_seq:
        return ChainVerification(
            cluster_uid,
            False,
            result.entries,
            result.head_seq,
            result.head_hash,
            failure_seq=previous.head_seq,
            reason="truncated",
            detail=f"the chain ends at seq {result.head_seq or 0}, below the verified head {previous.head_seq}",
        )
    page = await repo.chain_page(cluster_uid, previous.head_seq - 1, 1)
    if not page or page[0].hash != previous.head_hash:
        return ChainVerification(
            cluster_uid,
            False,
            result.entries,
            result.head_seq,
            result.head_hash,
            failure_seq=previous.head_seq,
            reason="head_rewritten",
            detail=f"the entry at the verified head seq {previous.head_seq} has another hash",
        )
    return None


async def verify_ledger(
    repo: LedgerRepository,
    *,
    now: Optional[datetime] = None,
    full: bool = False,
    checkpoints: Optional[Dict[str, Tuple[int, Optional[str], datetime]]] = None,
) -> Dict[str, ChainVerification]:
    """Verify every cluster's chain, record the status and count failures."""
    moment = now or datetime.now(timezone.utc)
    marks = _CHECKPOINTS if checkpoints is None else checkpoints
    results: Dict[str, ChainVerification] = {}
    total = 0
    for cluster_uid in await repo.clusters():
        previous = await repo.get_chain_status(cluster_uid)
        # The boundary between unkeyed and keyed entries counts only from a row signed with the current key.
        boundary = (
            previous.first_keyed_seq
            if previous is not None and repo.status_signature_state(previous) == "current"
            else None
        )
        checkpoint = marks.get(cluster_uid)
        incremental = not full and checkpoint is not None and moment - checkpoint[2] < FULL_VERIFICATION_INTERVAL
        if incremental:
            assert checkpoint is not None
            anchor = await repo.chain_page(cluster_uid, checkpoint[0] - 1, 1) if checkpoint[0] else []
            if checkpoint[0] and (not anchor or anchor[0].hash != checkpoint[1]):
                incremental = False
        if incremental:
            assert checkpoint is not None
            result = await verify_chain(
                repo, cluster_uid, after_seq=checkpoint[0], after_hash=checkpoint[1], first_keyed_seq=boundary
            )
        else:
            result = await verify_chain(repo, cluster_uid, first_keyed_seq=boundary)
        if result.ok:
            result = _status_failure(repo, result, previous, moment) or result
        if result.ok:
            result = await _tail_lost(repo, result, previous) or result
        if result.ok and not incremental:
            # The totals users read are not chained: the full pass re-derives them from the entries (LED-09).
            mismatch = await repo.totals_mismatch(cluster_uid)
            if mismatch is not None:
                result = replace(result, ok=False, reason="totals_mismatch", detail=mismatch)
        if result.ok:
            full_at = checkpoint[2] if incremental and checkpoint is not None else moment
            marks[cluster_uid] = (result.head_seq or 0, result.head_hash, full_at)
        else:
            marks.pop(cluster_uid, None)
            CHAIN_VERIFICATION_FAILURES.labels(reason=result.reason or "unknown").inc()
            logger.error(
                "Savings ledger chain of cluster %s failed verification at seq %s: %s (%s).",
                cluster_uid,
                result.failure_seq,
                result.reason,
                result.detail,
            )
        CHAIN_OK.labels(cluster_uid=cluster_uid).set(1 if result.ok else 0)
        if result.ok:
            _publish_head(cluster_uid, result.head_seq or 0, result.head_hash)
            CHAIN_HEAD_KEYED.labels(cluster_uid=cluster_uid).set(1 if result.head_keyed else 0)
        total += result.entries
        # A failure keeps the last head that passed as the anchor of the next verification.
        if result.ok:
            anchor_seq, anchor_hash = result.head_seq, result.head_hash
        elif previous is not None:
            anchor_seq, anchor_hash = previous.head_seq, previous.head_hash
        else:
            anchor_seq, anchor_hash = None, None
        await repo.record_chain_status(
            ChainStatus(
                cluster_uid=cluster_uid,
                verified_at=moment,
                ok=result.ok,
                entries=result.entries,
                head_seq=anchor_seq,
                head_hash=anchor_hash,
                failure=None if result.ok else f"{result.reason}: {result.detail}",
                head_keyed=result.head_keyed if result.ok else bool(previous and previous.head_keyed),
                first_keyed_seq=boundary
                if boundary is not None
                else (result.first_keyed_seq if result.ok and not incremental else None),
            ),
            # Only a trusted anchor is signed: never a forged, unsigned or missing one carried by a failure.
            sign=result.ok or (previous is not None and repo.status_signature_state(previous) == "current"),
        )
        results[cluster_uid] = result
    CHAIN_VERIFIED_ENTRIES.set(total)
    return results


async def accept_head(
    repo: LedgerRepository, cluster_uid: str, reason: str, *, now: Optional[datetime] = None
) -> ChainVerification:
    """Accept the cluster's current chain head as the new anchor, after an investigated incident (LED-05).

    Used after a backup restore or a failover that lost the last entries:
    the stored anchor is then ahead of the chain and every verification fails
    with ``truncated`` or ``head_rewritten``. The chain itself must still pass
    the structural checks (links, hashes, keys, reversals); otherwise nothing is
    accepted. The acceptance is logged with the operator's reason and the head
    it replaces; no ledger entry is written or changed.
    """
    if not reason.strip():
        raise ValueError("a reason is required to accept a new ledger head")
    moment = now or datetime.now(timezone.utc)
    result = await verify_chain(repo, cluster_uid)
    if not result.ok:
        return result
    previous = await repo.get_chain_status(cluster_uid)
    await repo.record_chain_status(
        ChainStatus(
            cluster_uid=cluster_uid,
            verified_at=moment,
            ok=True,
            entries=result.entries,
            head_seq=result.head_seq,
            head_hash=result.head_hash,
            failure=None,
            head_keyed=result.head_keyed,
            first_keyed_seq=result.first_keyed_seq,
        )
    )
    await repo.record_head_acceptance(
        cluster_uid,
        accepted_at=moment,
        reason=reason.strip(),
        previous=previous,
        head_seq=result.head_seq,
        head_hash=result.head_hash,
    )
    _CHECKPOINTS.pop(cluster_uid, None)
    logger.warning(
        "Savings ledger head of cluster %s accepted by an operator: seq %s (%s) replaces the anchor seq %s (%s). "
        "Reason: %s",
        cluster_uid,
        result.head_seq,
        result.head_hash,
        previous.head_seq if previous else None,
        previous.head_hash if previous else None,
        reason,
    )
    return result


_UNKEYED_WARNED = False


def warn_if_unkeyed(repo: LedgerRepository) -> bool:
    """Warn once per process when no ``LEDGER_CHAIN_KEY`` is configured; returns True when it warned."""
    global _UNKEYED_WARNED
    if _UNKEYED_WARNED:
        return False
    if repo.chain_key:
        if repo.key_since is None:
            _UNKEYED_WARNED = True
            logger.warning(
                "LEDGER_CHAIN_KEY is set without LEDGER_CHAIN_KEY_SINCE: a chain rewritten entirely without the key "
                "cannot be told from entries written before it. Set secrets.ledgerChainKeySince to the key's UTC "
                "creation time (LED-05)."
            )
            return True
        return False
    _UNKEYED_WARNED = True
    logger.warning(
        "LEDGER_CHAIN_KEY is not set: the savings ledger chain uses plain sha256, which anyone who can write to "
        "the database can recompute. Set secrets.ledgerChainKey (or let the chart generate it) to key it (LED-05)."
    )
    return True


async def run_ledger_chain() -> None:
    """Controller loop job: catch the v2 ledger up with the v1 ledger, verify its chains and reconcile (LED-04)."""
    from greenkube.storage.ledger_repository import get_ledger_repository
    from greenkube.verify.ledger.projector import build_projector

    warn_if_unkeyed(get_ledger_repository())

    result = await build_projector().sync(full=True)
    if result.written:
        logger.info(
            "Savings ledger v2: %d accrual(s), %d supersession reversal(s), %d restatement entries written.",
            result.projected,
            result.superseded,
            result.restated,
        )
    repo = get_ledger_repository()
    await verify_ledger(repo)
    if result.cluster_uid is not None and not result.skipped:
        from greenkube.core.savings_attributor import savings_cluster_name
        from greenkube.verify.ledger.reconciliation import reconcile

        try:
            await reconcile(repo, result.cluster_uid, savings_cluster_name())
        except Exception as exc:
            logger.error("Savings ledger reconciliation failed: %s", exc)


__all__ = [
    "accept_head",
    "CHAIN_OK",
    "CHAIN_VERIFICATION_FAILURES",
    "ChainVerification",
    "DEFAULT_INTERVAL_SECONDS",
    "run_ledger_chain",
    "verify_chain",
    "verify_ledger",
]
