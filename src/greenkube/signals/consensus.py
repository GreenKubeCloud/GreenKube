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
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from greenkube.signals.model import Capability, ContainerResourcesProposal, SourceSignal, proposal_identity


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

    In order: an explicit ``engine_family`` (attribute of the algorithm, or
    ``algorithm.params["engine_family"]``), the engine of a well-known
    algorithm (``vpa-recommender`` is ``vpa``), the engine of a well-known
    source (``goldilocks`` is ``vpa``), else the source id itself.
    """
    algorithm = signal.algorithm
    explicit = getattr(algorithm, "engine_family", None) or algorithm.params.get("engine_family")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    by_algorithm = ALGORITHM_ENGINE_FAMILIES.get(algorithm.name.strip().lower())
    if by_algorithm:
        return by_algorithm
    return SOURCE_ENGINE_FAMILIES.get(signal.source_id, signal.source_id)


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
    #: Independent votes (one per engine family).
    independent_votes: int = 1
    votes: List[ConsensusVote] = Field(default_factory=list)
    #: Sources that did not complete the scan and whose last known proposal (``{source_id: value}``)
    #: still supports the record (E8). Filled by the engine, never by :func:`combine`.
    last_known: Dict[str, int] = Field(default_factory=dict)

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
    """One ballot per engine family: a VPA object seen by VPA and Goldilocks is one vote."""
    by_family: "OrderedDict[str, List[Ballot]]" = OrderedDict()
    for ballot in ballots:
        by_family.setdefault(ballot.family, []).append(ballot)
    representatives = []
    for members in by_family.values():
        representatives.append(
            max(
                members,
                key=lambda ballot: (
                    ballot.payload is not None,
                    _priority(ballot, priorities),
                    ballot.value if ballot.value is not None else 0,
                    _reverse_text(ballot.source_id),
                ),
            )
        )
    return representatives


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
        independent_votes=len(representatives),
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
        independent_votes=len(representatives),
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
) -> List[ConsensusOutcome]:
    """One consensus per group of ballots."""
    return [
        combine(members, policy, priorities=priorities, weight_for=weight_for)
        for members in group_ballots(ballots).values()
    ]
