# src/greenkube/verify/ledger/verification.py
"""Hash-chain verification of the savings ledger v2 (roadmap C6, N7).

Each cluster's chain is walked in ``seq`` order. An entry passes when:

* its ``seq`` is the next position (a removed row leaves a gap);
* its ``prev_hash`` is the previous entry's ``hash`` (a reordered or forged row
  breaks the link);
* its ``hash`` equals ``sha256(prev_hash || canonical_json(entry))`` (a
  modified column changes it);
* its ``entry_id`` derives from its cluster and ``entry_key``.

The head verified last is stored in ``ledger_chain_status``: a chain that is
shorter than that head, or whose entry at that position has another hash,
lost its tail. Every failed verification increments
``greenkube_ledger_chain_verification_failures_total{reason}`` (alerted by the
chart's PrometheusRule) and is reported by ``GET /api/v2/ledger/summary``.

The controller loop ``ledger-chain`` first catches the v2 ledger up with the
v1 ledger (see :mod:`greenkube.verify.ledger.projector`), then verifies every
cluster's chain: incrementally from the last verified head on each tick, and
the whole chain once per :data:`FULL_VERIFICATION_INTERVAL` and on start.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from prometheus_client import Counter, Gauge

from greenkube.core.observability import REGISTRY as OBSERVABILITY_REGISTRY
from greenkube.storage.ledger_repository import ChainStatus, LedgerRepository
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


async def verify_chain(
    repo: LedgerRepository,
    cluster_uid: str,
    *,
    after_seq: int = 0,
    after_hash: Optional[str] = None,
    page_size: int = PAGE_SIZE,
) -> ChainVerification:
    """Walk the chain of ``cluster_uid`` after position ``after_seq`` (whose hash is ``after_hash``)."""
    expected_seq = after_seq + 1
    prev_hash = after_hash
    verified = after_seq
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
            elif entry.hash != entry.computed_hash():
                failure = ("hash_mismatch", f"seq {entry.seq} ({entry.entry_id}) was modified")
            elif entry.entry_id != entry_id_for(cluster_uid, entry.entry_key):
                failure = ("identity_mismatch", f"seq {entry.seq} has an entry id that does not match its key")
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
            verified = entry.seq
            expected_seq += 1
        if len(page) < page_size:
            break
    return ChainVerification(cluster_uid, True, verified, verified or None, prev_hash)


async def _tail_lost(
    repo: LedgerRepository, cluster_uid: str, result: ChainVerification
) -> Optional[ChainVerification]:
    """A chain shorter than the head verified before, or with another hash at that head, lost its tail."""
    previous = await repo.get_chain_status(cluster_uid)
    if previous is None or not previous.ok or previous.head_seq is None:
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
        checkpoint = marks.get(cluster_uid)
        incremental = not full and checkpoint is not None and moment - checkpoint[2] < FULL_VERIFICATION_INTERVAL
        if incremental:
            assert checkpoint is not None
            anchor = await repo.chain_page(cluster_uid, checkpoint[0] - 1, 1) if checkpoint[0] else []
            if checkpoint[0] and (not anchor or anchor[0].hash != checkpoint[1]):
                incremental = False
        if incremental:
            assert checkpoint is not None
            result = await verify_chain(repo, cluster_uid, after_seq=checkpoint[0], after_hash=checkpoint[1])
        else:
            result = await verify_chain(repo, cluster_uid)
        if result.ok:
            result = await _tail_lost(repo, cluster_uid, result) or result
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
        total += result.entries
        await repo.record_chain_status(
            ChainStatus(
                cluster_uid=cluster_uid,
                verified_at=moment,
                ok=result.ok,
                entries=result.entries,
                head_seq=result.head_seq,
                head_hash=result.head_hash,
                failure=None if result.ok else f"{result.reason}: {result.detail}",
            )
        )
        results[cluster_uid] = result
    CHAIN_VERIFIED_ENTRIES.set(total)
    return results


async def run_ledger_chain() -> None:
    """Controller loop job: catch the v2 ledger up with the v1 ledger, then verify its chains."""
    from greenkube.storage.ledger_repository import get_ledger_repository
    from greenkube.verify.ledger.projector import build_projector

    result = await build_projector().sync()
    if result.written:
        logger.info(
            "Savings ledger v2: %d accrual(s), %d supersession reversal(s), %d restatement entries written.",
            result.projected,
            result.superseded,
            result.restated,
        )
    await verify_ledger(get_ledger_repository())


__all__ = [
    "CHAIN_VERIFICATION_FAILURES",
    "ChainVerification",
    "DEFAULT_INTERVAL_SECONDS",
    "run_ledger_chain",
    "verify_chain",
    "verify_ledger",
]
