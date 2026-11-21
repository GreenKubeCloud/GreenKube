"""Consensus across sources (roadmap E6).

Several sources can propose a change to the same target: VPA, KRR and
GreenKube's own analyzer may each size the CPU request of one container. The
engine builds **one** recommendation per target, capability and finding kind
from all of them instead of keeping the highest-priority source and dropping
the others (the former ``dedup.arbitrate``).

* Signals are turned into :class:`Ballot` objects. A container-resources
  proposal is split per resource, so a source proposing CPU and memory in one
  signal votes in the CPU group and in the memory group, like the native CPU
  and memory analyzers.
* Sources that run the same engine vote once (:func:`engine_family`):
  Goldilocks runs the VPA recommender, so Goldilocks and VPA on the same VPA
  object are one vote, never two.
* Pushed signals (``push:<client>`` and the KRR/Polaris report uploads,
  origin ``push``) are capped: one counted ballot per push client, a pushed
  ballot never stands for an engine family another source also voted in, and
  pushed signals alone never produce a deletion (:func:`admissible_ballots`).
  Every pushed ballot of a group together is **one** independent vote
  (:func:`independent_vote_count`): one ``signals:write`` key can push as
  several clients and upload reports, so pushed sources never corroborate
  each other (the auto-approval decrease guard needs two independent votes).
* A :class:`VoteScreen` (roadmap A.5, F-12) sets aside the external numeric
  ballots that are not signals of need before any policy runs: a value on a
  known tool minimum (KRR ``simple`` 100 Mi, the Fairwinds VPA chart 100 Mi and
  15m) and a change below the minimum (5 % and 10m or 16 Mi). They are kept in
  :attr:`Consensus.ignored_votes`. A container-resources group without a
  native (GreenKube-measured) ballot never becomes a recommendation: an
  external source alone stays a signal.
* A :class:`ConsensusPolicy` picks the consolidated proposal:

  ``safest`` (default)
      the least aggressive value: the highest proposed request.
  ``median``
      the median per resource (the upper median for an even count, so the
      value is always one a source proposed, and the safer of the two).
  ``weighted``
      the (upper) median weighted by each source's measured track record.
  ``priority``
      the highest-priority source wins (``RECOMMENDATION_SOURCE_PRIORITY``),
      the behavior before consensus.

* :class:`Consensus` records how the value was chosen: the supporting
  sources, the agreement ``1 - (max - min) / max`` over the independent
  proposals, whether the consensus is contested (agreement below 0.5 or
  sources disagreeing on the direction of the change) and the per-source
  spread shown in the evidence ("VPA 300m · KRR 250m · GreenKube 280m → 300m").

Proposals that are not numeric (schedules, deletions, HPA settings, advisories)
are compared by identity: the proposal supported by most independent sources
wins, ties go to the higher-priority source.

This module is pure: it never reads storage or configuration.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from greenkube.signals.model import (
    Capability,
    ContainerResourcesProposal,
    DeletionProposal,
    SignalOrigin,
    SourceSignal,
    proposal_identity,
)


class ConsensusPolicy(StrEnum):
    """How the proposals of several sources on one target are combined."""

    SAFEST = "safest"
    MEDIAN = "median"
    WEIGHTED = "weighted"
    PRIORITY = "priority"


#: Accepted spellings of the policies (the roadmap names them "trust-weighted" and "source-priority").
POLICY_ALIASES: Dict[str, ConsensusPolicy] = {
    "trust-weighted": ConsensusPolicy.WEIGHTED,
    "source-priority": ConsensusPolicy.PRIORITY,
}

logger = logging.getLogger(__name__)

#: Agreement below which a consensus is contested.
CONTESTED_AGREEMENT = 0.5
#: Agreement at or above which a consensus of several independent sources raises confidence.
AGREEMENT_BONUS_THRESHOLD = 0.8
#: Confidence multiplier of a consensus of several independent sources that agree (capped at 1).
AGREEMENT_CONFIDENCE_BONUS = 1.1

#: Engine of well-known algorithms: sources running the same engine vote once.
ALGORITHM_ENGINE_FAMILIES: Dict[str, str] = {"vpa-recommender": "vpa"}
#: Engine of well-known sources.
SOURCE_ENGINE_FAMILIES: Dict[str, str] = {"goldilocks": "vpa"}
#: Source id prefix of the signals pushed through ``POST /api/v1/signals`` (G6).
PUSH_SOURCE_PREFIX = "push:"

#: v1 finding kind of each consolidated container request resource.
REQUEST_RESOURCE_KINDS: Dict[str, str] = {
    "cpu_request_m": "RIGHTSIZING_CPU",
    "memory_request_bytes": "RIGHTSIZING_MEMORY",
}
#: Group kind of each container limit resource (no v1 finding kind).
LIMIT_RESOURCE_KINDS: Dict[str, str] = {
    "cpu_limit_m": "cpu_limit_m",
    "memory_limit_bytes": "memory_limit_bytes",
}
#: Resource of each v1 container-request finding kind.
KIND_REQUEST_RESOURCES: Dict[str, str] = {kind: resource for resource, kind in REQUEST_RESOURCE_KINDS.items()}

#: ``(target key, capability, kind)``: one consolidated recommendation per group.
GroupKey = Tuple[str, str, str]


def parse_policy(value: Any) -> ConsensusPolicy:
    """The policy named by ``value`` (``safest`` when blank); ``ValueError`` when unknown."""
    text = str(getattr(value, "value", value) or "").strip().lower().replace("_", "-")
    if not text:
        return ConsensusPolicy.SAFEST
    if text in POLICY_ALIASES:
        return POLICY_ALIASES[text]
    try:
        return ConsensusPolicy(text)
    except ValueError:
        raise ValueError("RECOMMENDATION_CONSENSUS_POLICY must be safest, median, weighted or priority") from None


def engine_family(signal: SourceSignal) -> str:
    """The engine a signal comes from; signals of one engine on one target are a single vote.

    In order: the signal's ``engine_family`` (G1: the VPA connector sets
    ``vpa`` on VPA and Goldilocks signals; a push client may declare the
    engine it relays), an explicit ``engine_family`` of the algorithm (attribute
    or ``algorithm.params["engine_family"]``), the engine of a well-known
    algorithm (``vpa-recommender`` is ``vpa``), the engine of a well-known
    source (``goldilocks`` is ``vpa``), else the source id itself.
    """
    algorithm = signal.algorithm
    explicit = (
        signal.engine_family or getattr(algorithm, "engine_family", None) or algorithm.params.get("engine_family")
    )
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    by_algorithm = ALGORITHM_ENGINE_FAMILIES.get(algorithm.name.strip().lower())
    if by_algorithm:
        return by_algorithm
    return SOURCE_ENGINE_FAMILIES.get(signal.source_id, signal.source_id)


def is_pushed(signal: SourceSignal) -> bool:
    """Whether a signal was pushed by an external client (``push:<client>``, G6)."""
    return signal.provenance.origin == SignalOrigin.PUSH or signal.source_id.startswith(PUSH_SOURCE_PREFIX)


def _direction(value: Optional[int], current: Optional[int]) -> Optional[str]:
    if value is None or current is None:
        return None
    if value < current:
        return "decrease"
    if value > current:
        return "increase"
    return "unchanged"


@dataclass(frozen=True)
class Ballot:
    """One source's proposal on one group (a container-resources proposal votes once per resource)."""

    signal: SourceSignal
    #: Reconciliation and priority unit: the v1 source (``greenkube``, ``vpa``) or the source id.
    source: str
    family: str
    kind: str
    resource: Optional[str] = None
    value: Optional[int] = None
    current: Optional[int] = None
    #: Caller data carried through (the engine passes the source's v1 recommendation).
    payload: Any = None

    @property
    def source_id(self) -> str:
        return self.signal.source_id

    @property
    def group_key(self) -> GroupKey:
        return (self.signal.target.key(), str(self.signal.capability), self.kind)

    @property
    def numeric(self) -> bool:
        return self.resource is not None and self.value is not None

    @property
    def pushed(self) -> bool:
        return is_pushed(self.signal)

    @property
    def deletion(self) -> bool:
        return isinstance(self.signal.proposal, DeletionProposal)

    @property
    def direction(self) -> Optional[str]:
        return _direction(self.value, self.current)

    @property
    def identity(self) -> str:
        """What the ballot proposes (``current`` and explanations excluded)."""
        if self.numeric:
            return f"{self.resource}={self.value}"
        return json.dumps(proposal_identity(self.signal.proposal), sort_keys=True, separators=(",", ":"), default=str)


def ballots_for_signal(
    signal: SourceSignal,
    *,
    source: Optional[str] = None,
    payload: Any = None,
    payload_kind: Optional[str] = None,
) -> List[Ballot]:
    """The ballots of one signal.

    A container-resources proposal votes once per proposed resource (requests
    in the ``RIGHTSIZING_CPU``/``RIGHTSIZING_MEMORY`` groups, limits in their
    own groups). ``payload_kind`` restricts it to the resource of a v1 finding
    kind, so a built-in source's v1 recommendation is only ever the base of
    its own group. Any other proposal is one ballot in its finding kind's group.
    """
    group_source = source or signal.provenance.legacy_source or signal.source_id
    family = engine_family(signal)
    proposal = signal.proposal
    if isinstance(proposal, ContainerResourcesProposal) and signal.capability in (
        Capability.CONTAINER_REQUESTS,
        Capability.CONTAINER_LIMITS,
    ):
        resource_kinds = (
            REQUEST_RESOURCE_KINDS if signal.capability == Capability.CONTAINER_REQUESTS else LIMIT_RESOURCE_KINDS
        )
        ballots: List[Ballot] = []
        for resource, kind in resource_kinds.items():
            if payload_kind is not None and payload_kind != kind:
                continue
            value = getattr(proposal.proposed, resource)
            if value is None:
                continue
            ballots.append(
                Ballot(
                    signal=signal,
                    source=group_source,
                    family=family,
                    kind=kind,
                    resource=resource,
                    value=int(value),
                    current=getattr(proposal.current, resource),
                    payload=payload,
                )
            )
        if ballots:
            return ballots
    return [Ballot(signal=signal, source=group_source, family=family, kind=signal.kind, payload=payload)]


#: Reconciliation unit of the native analyzers (``greenkube``, ``greenkube.<analyzer>``).
NATIVE_SOURCE = "greenkube"

#: Capabilities whose groups need a native ballot to become a recommendation (A.5).
NATIVE_MEASURED_CAPABILITIES = frozenset({Capability.CONTAINER_REQUESTS, Capability.CONTAINER_LIMITS})

#: Known minimum each tool applies to its proposals, per engine family (``SIGNAL_TOOL_FLOORS``):
#: KRR ``simple`` (``--mem-min 100``, ``--cpu-min 10``) and the Fairwinds VPA chart
#: (``pod-recommendation-min-memory-mb: 100``, ``pod-recommendation-min-cpu-millicores: 15``).
DEFAULT_TOOL_FLOORS: Dict[str, Dict[str, int]] = {
    "krr": {"cpu_request_m": 10, "memory_request_bytes": 100 * 1024 * 1024},
    "vpa": {"cpu_request_m": 15, "memory_request_bytes": 100 * 1024 * 1024},
}
#: Families whose floor applies per pod and is split evenly between its containers (the VPA recommender).
PER_POD_FLOOR_FAMILIES = frozenset({"vpa"})
#: Largest container count a per-pod floor is divided by when the signal does not say.
MAX_POD_CONTAINERS = 8

#: Why an external ballot was set aside.
IGNORED_TOOL_FLOOR = "tool_floor"
IGNORED_MIN_CHANGE = "below_min_change"
IGNORED_MATURITY_UNKNOWN = "maturity_unknown"


def maturity_unknown(ballot: "Ballot") -> bool:
    """Whether the signal says its source's maturity could not be read (``raw.maturity.state``)."""
    raw = ballot.signal.raw if isinstance(ballot.signal.raw, dict) else {}
    maturity = raw.get("maturity")
    return isinstance(maturity, dict) and maturity.get("state") == "unknown"


def is_native(ballot: "Ballot") -> bool:
    """Whether a ballot comes from GreenKube's own measurement.

    A native analyzer (``greenkube``, ``greenkube.<analyzer>``), or a value a
    built-in connector computed from GreenKube's usage data under a
    ``greenkube.*`` engine family (the Polaris missing-request follow-up). A
    pushed signal is never native, whatever engine family it declares.
    """
    if ballot.source == NATIVE_SOURCE or ballot.source.startswith(NATIVE_SOURCE + "."):
        return True
    return not ballot.pushed and ballot.family.startswith(NATIVE_SOURCE + ".")


@dataclass(frozen=True)
class VoteScreen:
    """Which external numeric ballots are not signals of need (roadmap A.5, F-12).

    ``floors`` maps an engine family (or a source id) to the known minimum of
    the tool per resource (``cpu_request_m``, ``memory_request_bytes``): a value
    equal to it is the tool's floor, not a measurement. ``min_change_ratio``
    and ``min_change`` (per resource) set the smallest change that counts as a
    vote: both must be reached. Native ballots are never screened.
    """

    floors: Mapping[str, Mapping[str, int]] = field(default_factory=lambda: DEFAULT_TOOL_FLOORS)
    min_change_ratio: float = 0.05
    min_change: Mapping[str, int] = field(
        default_factory=lambda: {"cpu_request_m": 10, "memory_request_bytes": 16 * 1024 * 1024}
    )
    #: A container-resources group without a native ballot never becomes a recommendation.
    require_native: bool = True

    def floor_for(self, ballot: "Ballot") -> Optional[int]:
        """The known tool minimum of a ballot's resource, or None."""
        for key in (ballot.family, ballot.source_id, ballot.source):
            floors = self.floors.get(key)
            if floors is not None:
                value = floors.get(str(ballot.resource))
                return int(value) if value else None
        return None

    def on_floor(self, ballot: "Ballot") -> bool:
        """Whether the ballot's value is its tool's known minimum (split per container for a per-pod floor)."""
        floor = self.floor_for(ballot)
        if floor is None or ballot.value is None:
            return False
        if ballot.value <= floor and ballot.family not in PER_POD_FLOOR_FAMILIES:
            return True
        raw = ballot.signal.raw if isinstance(ballot.signal.raw, dict) else {}
        containers = raw.get("containers")
        counts = [int(containers)] if isinstance(containers, int) and containers > 0 else []
        if ballot.family in PER_POD_FLOOR_FAMILIES and not counts:
            counts = list(range(1, MAX_POD_CONTAINERS + 1))
        counts = counts or [1]
        for count in counts:
            share = floor / count
            # The recommender rounds a split floor up to the next unit (7.5m -> 8m).
            if abs(ballot.value - share) <= max(1.0, share * 0.01):
                return True
        return False

    def below_min_change(self, ballot: "Ballot") -> bool:
        """Whether the ballot changes its current value by less than the minimum.

        A proposal equal to the current value is a "keep it" vote, not a small
        change: it still counts (under ``safest`` it holds the current request).
        """
        if ballot.value is None or not ballot.current:
            return False
        delta = abs(ballot.value - ballot.current)
        if delta == 0:
            return False
        minimum = max(self.min_change_ratio * ballot.current, float(self.min_change.get(str(ballot.resource), 0)))
        return delta < minimum

    def reason(self, ballot: "Ballot", policy: Optional["ConsensusPolicy"] = None) -> Optional[str]:
        """Why an external numeric ballot is not counted as proposed, or None when it is a vote.

        ``maturity_unknown``: a decrease from a source whose maturity could not
        be read (a VPA without checkpoints) may only hold or raise a value, so
        it is dropped under every policy but ``safest`` (where a lower value
        never wins anyway).
        """
        if is_native(ballot) or not ballot.numeric:
            return None
        if self.on_floor(ballot):
            return IGNORED_TOOL_FLOOR
        if (
            policy is not None
            and policy != ConsensusPolicy.SAFEST
            and maturity_unknown(ballot)
            and ballot.value is not None
            and ballot.current is not None
            and ballot.value < ballot.current
        ):
            return IGNORED_MATURITY_UNKNOWN
        if self.below_min_change(ballot):
            return IGNORED_MIN_CHANGE
        return None


class IgnoredVote(BaseModel):
    """An external proposal that was not counted (a tool floor, an insignificant change)."""

    model_config = ConfigDict(extra="ignore")

    source_id: str
    signal_id: str
    value: Optional[int] = None
    #: ``tool_floor``, ``below_min_change`` (still a "keep" vote at the current value) or ``maturity_unknown``.
    reason: str


class ConsensusVote(BaseModel):
    """One signal's part in a consensus."""

    model_config = ConfigDict(extra="ignore")

    source_id: str
    #: Reconciliation unit of the source (``greenkube`` for native analyzers).
    source: str
    family: str
    signal_id: str
    value: Optional[int] = None
    direction: Optional[str] = None
    #: False when another source of the same engine family already voted.
    counted: bool = True
    #: Whether this signal supports the chosen proposal.
    supports_chosen: bool = False
    #: Pushed through the API (``push:<client>``, KRR/Polaris reports): all pushed votes are one independent vote.
    pushed: bool = False


class Consensus(BaseModel):
    """How the consolidated proposal of one recommendation was chosen (persisted as JSON)."""

    model_config = ConfigDict(extra="ignore")

    policy: str
    #: Resource of a numeric consensus (``cpu_request_m``...); None for other proposals.
    resource: Optional[str] = None
    #: Consolidated value of a numeric consensus.
    value: Optional[int] = None
    #: Source ids that support the chosen proposal.
    chosen_from: List[str] = Field(default_factory=list)
    #: 1 - (max - min) / max over the independent proposals (numeric), else the share supporting the choice.
    agreement: float = 1.0
    #: Agreement below 0.5 or sources disagreeing on the direction: never auto-approved.
    contested: bool = False
    reasons: List[str] = Field(default_factory=list)
    #: Per resource: ``{source_id: value}``.
    spread: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    #: Independent votes: one per engine family, all pushed signals together counting once.
    independent_votes: int = 1
    votes: List[ConsensusVote] = Field(default_factory=list)
    #: Every source id that voted on the group, the chosen one first (provenance badges:
    #: ``goldilocks``, ``krr``, ``push:<client>``...), family duplicates included.
    source_ids: List[str] = Field(default_factory=list)
    #: Sources that did not complete the scan and whose last known proposal (``{source_id: value}``)
    #: still supports the record (E8). Filled by the engine, never by :func:`combine`.
    last_known: Dict[str, int] = Field(default_factory=dict)
    #: External proposals set aside before the policy ran (tool floors, insignificant changes, A.5).
    ignored_votes: List[IgnoredVote] = Field(default_factory=list)

    @property
    def confidence_bonus(self) -> float:
        """Confidence multiplier earned by agreement between several independent sources."""
        if self.independent_votes >= 2 and not self.contested and self.agreement >= AGREEMENT_BONUS_THRESHOLD:
            return AGREEMENT_CONFIDENCE_BONUS
        return 1.0

    def summary(self, format_value: Optional[Callable[[str, int], str]] = None) -> str:
        """``vpa 300m · krr 250m · greenkube.rightsizing_cpu 280m → 300m (safest, agreement 0.83)``."""
        fmt = format_value or _format_value
        if self.resource is None or self.value is None:
            sources = ", ".join(self.chosen_from) or "no source"
            return f"{self.policy}: {sources} ({self.independent_votes} independent, agreement {self.agreement:.2f})"
        parts = [
            f"{source_id} {fmt(self.resource, int(value))}"
            for source_id, value in (self.spread.get(self.resource) or {}).items()
            if isinstance(value, (int, float))
        ]
        text = " · ".join(parts) + f" → {fmt(self.resource, self.value)}"
        text += f" ({self.policy}, agreement {self.agreement:.2f}"
        return text + (", contested)" if self.contested else ")")


def _format_value(resource: str, value: int) -> str:
    if resource.startswith("cpu"):
        return f"{value}m"
    if resource.startswith("memory"):
        return f"{value / (1024 * 1024):.0f}Mi"
    return str(value)


@dataclass
class ConsensusOutcome:
    """The consensus of one group and the ballot whose proposal it chose."""

    key: GroupKey
    consensus: Consensus
    #: The representative ballot whose proposal (value) was chosen.
    chosen: Ballot
    #: Every ballot of the group, family duplicates included.
    ballots: List[Ballot] = field(default_factory=list)
    #: One ballot per engine family (the counted votes).
    representatives: List[Ballot] = field(default_factory=list)

    @property
    def sources(self) -> List[str]:
        """Reconciliation units supporting the group: the chosen one first, then by order of appearance."""
        ordered = [self.chosen.source]
        for ballot in self.ballots:
            if ballot.source not in ordered:
                ordered.append(ballot.source)
        return ordered


def _priority(ballot: Ballot, priorities: Mapping[str, int]) -> int:
    return int(priorities.get(ballot.source, priorities.get(ballot.source_id, 0)))


def _preference(ballot: Ballot, priorities: Mapping[str, int]) -> tuple:
    """Higher sorts first: priority, a v1 recommendation to build on, then a stable source order."""
    return (_priority(ballot, priorities), ballot.payload is not None, _reverse_text(ballot.source_id))


def _reverse_text(text: str) -> Tuple[int, ...]:
    # Sorting descending on this tuple orders the texts ascending.
    return tuple(-ord(char) for char in text)


def _representatives(ballots: Sequence[Ballot], priorities: Mapping[str, int]) -> List[Ballot]:
    """One ballot per engine family, and at most one counted ballot per push client.

    A VPA object seen by VPA and Goldilocks is one vote. A pushed ballot never
    stands for a family another source voted in (a client relaying VPA advice
    adds no vote and cannot replace VPA's value), and a push client counts once
    whatever engine families its signals declare.
    """
    by_family: "OrderedDict[str, List[Ballot]]" = OrderedDict()
    for ballot in ballots:
        by_family.setdefault(ballot.family, []).append(ballot)
    representatives = []
    pushed_clients: set = set()
    for members in by_family.values():
        representative = max(
            members,
            key=lambda ballot: (
                not ballot.pushed,
                ballot.payload is not None,
                _priority(ballot, priorities),
                ballot.value if ballot.value is not None else 0,
                _reverse_text(ballot.source_id),
            ),
        )
        if representative.pushed:
            if representative.source_id in pushed_clients:
                continue
            pushed_clients.add(representative.source_id)
        representatives.append(representative)
    return representatives


def independent_vote_count(representatives: Sequence[Ballot]) -> int:
    """Independent votes among the counted ballots: every pushed ballot together counts once.

    Push clients and the KRR/Polaris report endpoints share one trust boundary
    (any ``signals:write`` key, or the principal behind it), so they never
    corroborate each other: two clients of one key, or a client and a report,
    are a single independent vote. Their proposals still shape the value.
    """
    native = sum(1 for ballot in representatives if not ballot.pushed)
    return native + (1 if any(ballot.pushed for ballot in representatives) else 0)


def admissible_ballots(ballots: Sequence[Ballot]) -> List[Ballot]:
    """The ballots of one group that may take part in its consensus.

    Pushed signals alone never produce a deletion: a pushed deletion counts
    only as support for a deletion another (non-pushed) source proposes on
    the same target. Everything else is admissible.
    """
    if any(ballot.deletion and not ballot.pushed for ballot in ballots):
        return list(ballots)
    admitted = [ballot for ballot in ballots if not (ballot.deletion and ballot.pushed)]
    if len(admitted) != len(ballots):
        logger.info(
            "Ignoring %d pushed deletion signal(s) on %s: no other source proposes the deletion.",
            len(ballots) - len(admitted),
            ballots[0].group_key[0],
        )
    return admitted


def _upper_weighted_median(pairs: Sequence[Tuple[int, float]]) -> int:
    """Smallest value such that the weight at or above it reaches half the total (the safer median)."""
    ordered = sorted(pairs, key=lambda pair: pair[0], reverse=True)
    total = sum(weight for _value, weight in ordered)
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= total / 2:
            return value
    return ordered[-1][0]


def _weight(ballot: Ballot, weight_for: Optional[Callable[[Ballot], float]]) -> float:
    if weight_for is None:
        return 1.0
    try:
        weight = float(weight_for(ballot))
    except Exception:
        return 1.0
    return weight if weight > 0 else 1e-6


def combine(
    ballots: Sequence[Ballot],
    policy: ConsensusPolicy | str = ConsensusPolicy.SAFEST,
    *,
    priorities: Optional[Mapping[str, int]] = None,
    weight_for: Optional[Callable[[Ballot], float]] = None,
) -> ConsensusOutcome:
    """The consensus of the ballots of one group (all ballots must share a group key)."""
    if not ballots:
        raise ValueError("a consensus needs at least one ballot")
    policy = parse_policy(policy)
    priorities = priorities or {}
    representatives = _representatives(ballots, priorities)
    numeric = all(ballot.numeric for ballot in representatives)
    if numeric:
        chosen, consensus = _numeric(representatives, ballots, policy, priorities, weight_for)
    else:
        chosen, consensus = _categorical(representatives, ballots, policy, priorities, weight_for)
    consensus.source_ids = _unique([chosen.source_id, *(ballot.source_id for ballot in ballots)])
    return ConsensusOutcome(
        key=ballots[0].group_key,
        consensus=consensus,
        chosen=chosen,
        ballots=list(ballots),
        representatives=representatives,
    )


def _votes(ballots: Sequence[Ballot], representatives: Sequence[Ballot], supports) -> List[ConsensusVote]:
    counted = {id(ballot) for ballot in representatives}
    return [
        ConsensusVote(
            source_id=ballot.source_id,
            source=ballot.source,
            family=ballot.family,
            signal_id=ballot.signal.signal_id,
            value=ballot.value,
            direction=ballot.direction,
            counted=id(ballot) in counted,
            supports_chosen=supports(ballot),
            pushed=ballot.pushed,
        )
        for ballot in ballots
    ]


def _numeric(
    representatives: Sequence[Ballot],
    ballots: Sequence[Ballot],
    policy: ConsensusPolicy,
    priorities: Mapping[str, int],
    weight_for: Optional[Callable[[Ballot], float]],
) -> Tuple[Ballot, Consensus]:
    values = [int(ballot.value) for ballot in representatives if ballot.value is not None]
    if policy == ConsensusPolicy.PRIORITY:
        value = int(max(representatives, key=lambda ballot: _preference(ballot, priorities)).value or 0)
    elif policy == ConsensusPolicy.MEDIAN:
        value = sorted(values, reverse=True)[(len(values) - 1) // 2]
    elif policy == ConsensusPolicy.WEIGHTED:
        value = _upper_weighted_median(
            [(int(ballot.value or 0), _weight(ballot, weight_for)) for ballot in representatives]
        )
    else:
        value = max(values)
    chosen = max(
        (ballot for ballot in representatives if ballot.value == value),
        key=lambda ballot: _preference(ballot, priorities),
    )
    highest, lowest = max(values), min(values)
    agreement = 1.0 if highest <= 0 else 1.0 - (highest - lowest) / highest
    agreement = round(max(0.0, min(1.0, agreement)), 4)
    reasons: List[str] = []
    if agreement < CONTESTED_AGREEMENT:
        reasons.append(f"agreement {agreement:.2f} is below {CONTESTED_AGREEMENT:.1f}")
    directions: Dict[str, List[str]] = {}
    for ballot in representatives:
        current = ballot.current if ballot.current is not None else chosen.current
        direction = _direction(ballot.value, current)
        if direction in ("decrease", "increase"):
            directions.setdefault(direction, []).append(ballot.source_id)
    if len(directions) > 1:
        reasons.append(
            "sources disagree on the direction: "
            + "; ".join(f"{name} ({', '.join(sources)})" for name, sources in sorted(directions.items()))
        )
    resource = str(chosen.resource)
    consensus = Consensus(
        policy=policy.value,
        resource=resource,
        value=value,
        chosen_from=_unique(ballot.source_id for ballot in ballots if ballot.value == value),
        agreement=agreement,
        contested=bool(reasons),
        reasons=reasons,
        spread={resource: {ballot.source_id: ballot.value for ballot in ballots}},
        independent_votes=independent_vote_count(representatives),
        votes=_votes(ballots, representatives, lambda ballot: ballot.value == value),
    )
    return chosen, consensus


def _categorical(
    representatives: Sequence[Ballot],
    ballots: Sequence[Ballot],
    policy: ConsensusPolicy,
    priorities: Mapping[str, int],
    weight_for: Optional[Callable[[Ballot], float]],
) -> Tuple[Ballot, Consensus]:
    support: Dict[str, float] = {}
    for ballot in representatives:
        weight = _weight(ballot, weight_for) if policy == ConsensusPolicy.WEIGHTED else 1.0
        support[ballot.identity] = support.get(ballot.identity, 0.0) + weight
    if policy == ConsensusPolicy.PRIORITY:
        chosen = max(representatives, key=lambda ballot: _preference(ballot, priorities))
    else:
        chosen = max(
            representatives,
            key=lambda ballot: (support[ballot.identity], _preference(ballot, priorities)),
        )
    total = sum(support.values()) or 1.0
    agreement = round(support[chosen.identity] / total, 4)
    reasons = (
        [f"agreement {agreement:.2f} is below {CONTESTED_AGREEMENT:.1f}"] if agreement < CONTESTED_AGREEMENT else []
    )
    identity = chosen.identity
    consensus = Consensus(
        policy=policy.value,
        chosen_from=_unique(ballot.source_id for ballot in ballots if ballot.identity == identity),
        agreement=agreement,
        contested=bool(reasons),
        reasons=reasons,
        independent_votes=independent_vote_count(representatives),
        votes=_votes(ballots, representatives, lambda ballot: ballot.identity == identity),
    )
    return chosen, consensus


def _unique(items) -> List[str]:
    result: List[str] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def group_ballots(ballots: Sequence[Ballot]) -> "OrderedDict[GroupKey, List[Ballot]]":
    """Ballots by group, in order of first appearance."""
    groups: "OrderedDict[GroupKey, List[Ballot]]" = OrderedDict()
    for ballot in ballots:
        groups.setdefault(ballot.group_key, []).append(ballot)
    return groups


def consolidate(
    ballots: Sequence[Ballot],
    policy: ConsensusPolicy | str = ConsensusPolicy.SAFEST,
    *,
    priorities: Optional[Mapping[str, int]] = None,
    weight_for: Optional[Callable[[Ballot], float]] = None,
    screen: Optional[VoteScreen] = None,
) -> List[ConsensusOutcome]:
    """One consensus per group of ballots.

    Pushed-only deletions are dropped (:func:`admissible_ballots`). With a
    ``screen``, external ballots that are not signals of need are listed in
    ``ignored_votes``: a tool floor or a decrease of unknown maturity under a
    policy other than ``safest`` is dropped, a change below the minimum votes
    "keep the current value". A container-resources group without a native
    ballot produces no outcome: its signals stay signals.
    """
    outcomes = []
    source_only = 0
    parsed_policy = parse_policy(policy)
    for members in group_ballots(ballots).values():
        admitted = admissible_ballots(members)
        if not admitted:
            continue
        ignored: List[IgnoredVote] = []
        if screen is not None:
            if (
                screen.require_native
                and admitted[0].signal.capability in NATIVE_MEASURED_CAPABILITIES
                and not any(is_native(ballot) for ballot in admitted)
            ):
                source_only += 1
                continue
            kept = []
            for ballot in admitted:
                reason = screen.reason(ballot, parsed_policy)
                if reason is None:
                    kept.append(ballot)
                    continue
                ignored.append(
                    IgnoredVote(
                        source_id=ballot.source_id,
                        signal_id=ballot.signal.signal_id,
                        value=ballot.value,
                        reason=reason,
                    )
                )
                if reason == IGNORED_MIN_CHANGE:
                    # A small change is a "keep it" vote: it counts at the current value.
                    kept.append(replace(ballot, value=ballot.current))
            admitted = kept
            if not admitted:
                continue
        outcome = combine(admitted, policy, priorities=priorities, weight_for=weight_for)
        outcome.consensus.ignored_votes = ignored
        outcomes.append(outcome)
    if source_only:
        logger.info(
            "%d container-resources group(s) proposed by external sources only, with no native measurement: "
            "kept as signals, not recommendations.",
            source_only,
        )
    return outcomes
