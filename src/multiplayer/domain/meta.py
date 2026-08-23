"""The Meta read vocabulary: what may be asked, and when an answer is still current.

Three passes, in this order, and the order is the design. Surveillance and
productivity markers refuse first, so a question tripping them can never reach a
kind. Then an exact match against a curated corpus of accepted forms. Anything
else refuses. There is no nearest-kind fallback, ever: an ordered table of
compiled patterns was measured against the refusal corpus and made 8 of 11
questions answer with the *wrong* kind rather than refuse, and answering
confidently about the wrong thing is worse than refusing.
"""

from __future__ import annotations

import re
from enum import StrEnum

from .events import EventType
from .models import DomainError, OntologyEntityKind

REFUSAL_PREFIX = "unsupported Meta question"


class MetaQuestionKind(StrEnum):
    STATUS = "STATUS"
    BLOCKERS = "BLOCKERS"
    CHANGES = "CHANGES"
    DECISIONS = "DECISIONS"
    DISAGREEMENT = "DISAGREEMENT"
    WHY_DECISION = "WHY_DECISION"
    DECISION_EVIDENCE = "DECISION_EVIDENCE"


class MetaAnswerStatus(StrEnum):
    ANSWERED = "ANSWERED"
    ANSWERED_UNCONFIRMED_ONLY = "ANSWERED_UNCONFIRMED_ONLY"
    REFUSED = "REFUSED"


class MetaRefusalReason(StrEnum):
    """Why an authorized query returned nothing, without disclosing content."""

    NO_ASSERTIONS_IN_SCOPE = "NO_ASSERTIONS_IN_SCOPE"
    NO_AUTHORIZED_EVIDENCE = "NO_AUTHORIZED_EVIDENCE"


class OntologyAssurance(StrEnum):
    CONFIRMED = "CONFIRMED"
    SYSTEM_MATERIALIZED = "SYSTEM_MATERIALIZED"
    UNCONFIRMED_AI = "UNCONFIRMED_AI"


def normalize_question(question: str) -> str:
    """Exactly the normalization the two decision kinds have always used."""
    return " ".join(question.strip().lower().split()).rstrip("?!. ")


def _phrases(*phrases: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(re.escape(phrase) for phrase in phrases) + r")\b")


# A person reference only refuses inside a comparative or a superlative: "who
# owns this decision" is a legitimate question, "who owns the most" is a
# per-person score by another name.
_PERSON_REFERENCE = _phrases(
    "who", "whom", "employee", "employees", "individual", "individuals", "teammate", "teammates"
)
_COMPARATIVE = _phrases(
    "most", "least", "more", "fewer", "hardest", "best", "worst", "top", "compared to"
)
_RANKING = _phrases(
    "rank",
    "ranked",
    "ranks",
    "ranking",
    "rankings",
    "leaderboard",
    "leaderboards",
    "scoreboard",
    "scoreboards",
    "standings",
)
_TOP_N = re.compile(r"\btop \d+\b")
_OUTPUT_VOLUME = _phrases("commit", "commits", "lines of code", "volume", "throughput", "velocity")
_HOW_MANY = re.compile(r"\bhow many (?:messages|outputs|runs)\b")
_PRODUCTIVITY = _phrases(
    "productivity",
    "productive",
    "performance",
    "activity",
    "engagement",
    "contribution",
    "contributions",
    "effort",
    "efforts",
    "worked hardest",
)


def _bears_surveillance_marker(normalized: str) -> bool:
    """Whether the question asks for an activity, volume, ranking or productivity figure."""
    if _PERSON_REFERENCE.search(normalized) and _COMPARATIVE.search(normalized):
        return True
    if _RANKING.search(normalized) or _TOP_N.search(normalized):
        return True
    if _OUTPUT_VOLUME.search(normalized) or _HOW_MANY.search(normalized):
        return True
    return bool(_PRODUCTIVITY.search(normalized))


# Every accepted form, normalized. A form belongs to exactly one kind, so a new
# form cannot silently widen a neighbouring kind.
ACCEPTED_QUESTIONS: dict[str, MetaQuestionKind] = {
    "status": MetaQuestionKind.STATUS,
    "what is the status": MetaQuestionKind.STATUS,
    "what is the current status": MetaQuestionKind.STATUS,
    "where do things stand": MetaQuestionKind.STATUS,
    "blockers": MetaQuestionKind.BLOCKERS,
    "what is blocking": MetaQuestionKind.BLOCKERS,
    "what is blocked": MetaQuestionKind.BLOCKERS,
    "what are the blockers": MetaQuestionKind.BLOCKERS,
    "changes": MetaQuestionKind.CHANGES,
    "what changed": MetaQuestionKind.CHANGES,
    "what changed this week": MetaQuestionKind.CHANGES,
    "what has changed recently": MetaQuestionKind.CHANGES,
    "decisions": MetaQuestionKind.DECISIONS,
    "what decisions require attention": MetaQuestionKind.DECISIONS,
    "which decisions need review": MetaQuestionKind.DECISIONS,
    "disagreement": MetaQuestionKind.DISAGREEMENT,
    "where is the disagreement": MetaQuestionKind.DISAGREEMENT,
    "what is contested": MetaQuestionKind.DISAGREEMENT,
    "where do the agents disagree": MetaQuestionKind.DISAGREEMENT,
    "why": MetaQuestionKind.WHY_DECISION,
    "why_decision": MetaQuestionKind.WHY_DECISION,
    "why decision": MetaQuestionKind.WHY_DECISION,
    "why was this decision made": MetaQuestionKind.WHY_DECISION,
    "why was the decision made": MetaQuestionKind.WHY_DECISION,
    "what is the reason for this decision": MetaQuestionKind.WHY_DECISION,
    "what are the reasons for this decision": MetaQuestionKind.WHY_DECISION,
    "evidence": MetaQuestionKind.DECISION_EVIDENCE,
    "decision_evidence": MetaQuestionKind.DECISION_EVIDENCE,
    "decision evidence": MetaQuestionKind.DECISION_EVIDENCE,
    "what evidence supports this decision": MetaQuestionKind.DECISION_EVIDENCE,
    "what evidence supports the decision": MetaQuestionKind.DECISION_EVIDENCE,
    "show supporting evidence": MetaQuestionKind.DECISION_EVIDENCE,
    "show the evidence for this decision": MetaQuestionKind.DECISION_EVIDENCE,
    "what sources support this decision": MetaQuestionKind.DECISION_EVIDENCE,
}

DECISION_KINDS = frozenset({MetaQuestionKind.WHY_DECISION, MetaQuestionKind.DECISION_EVIDENCE})


def classify_meta_question(question: str) -> MetaQuestionKind:
    """Refuse first, match exactly second, refuse again otherwise."""
    normalized = normalize_question(question)
    if _bears_surveillance_marker(normalized):
        raise DomainError(
            f"{REFUSAL_PREFIX}; this workspace does not answer for individual activity, "
            "output volume, ranking or productivity"
        )
    kind = ACCEPTED_QUESTIONS.get(normalized)
    if kind is None:
        raise DomainError(
            f"{REFUSAL_PREFIX}; ask about status, blockers, changes, decisions, "
            "disagreement, why the decision was made, or what evidence supports it"
        )
    return kind


# ── Derived currency ─────────────────────────────────────────────────────────

# An assertion written at sequence A is current when no event in (A, head]
# belongs to its invalidation class. Each class errs wide: a false "as-of" costs
# a caveat, a false "current" costs a wrong answer.
_SUPERSEDED = (EventType.ONTOLOGY_ASSERTION_SUPERSEDED,)

_INVALIDATION_CLASSES: dict[OntologyEntityKind, tuple[EventType, ...]] = {
    OntologyEntityKind.TASK: (
        EventType.TASK_CREATED,
        EventType.TASK_ASSIGNED,
        EventType.TASK_UNASSIGNED,
        EventType.TASK_STARTED,
        EventType.TASK_COMPLETED,
        EventType.TASK_FAILED,
        EventType.TASK_CANCELLED,
        EventType.TASK_DELEGATED,
        *_SUPERSEDED,
    ),
    OntologyEntityKind.DECISION: (
        EventType.DECISION_CREATED,
        EventType.DECISION_UPDATED,
        EventType.DECISION_SUPERSEDED,
        EventType.ARTIFACT_VERSION_CREATED,
        EventType.SYNTHESIS_PUBLISHED,
        *_SUPERSEDED,
    ),
    OntologyEntityKind.ARTIFACT: (
        EventType.ARTIFACT_CREATED,
        EventType.ARTIFACT_UPDATED,
        EventType.ARTIFACT_VERSION_CREATED,
        EventType.SYNTHESIS_PUBLISHED,
        *_SUPERSEDED,
    ),
    OntologyEntityKind.CLAIM: (
        EventType.AGENT_OUTPUT_CREATED,
        EventType.OUTPUT_SELECTION_UPDATED,
        EventType.BRANCH_SYNTHESIS_COMPLETED,
        *_SUPERSEDED,
    ),
    OntologyEntityKind.AGENT_OUTPUT: (
        EventType.AGENT_OUTPUT_CREATED,
        EventType.OUTPUT_SELECTION_UPDATED,
        EventType.BRANCH_SYNTHESIS_COMPLETED,
        *_SUPERSEDED,
    ),
    OntologyEntityKind.PERSON: (
        EventType.USER_JOINED_ROOM,
        EventType.USER_LEFT_ROOM,
        EventType.USER_ROLE_CHANGED,
        EventType.USER_REMOVED_ROOM,
        *_SUPERSEDED,
    ),
    OntologyEntityKind.PROJECT: (
        EventType.ROOM_UPDATED,
        EventType.ROOM_ARCHIVED,
        *_SUPERSEDED,
    ),
}


def invalidation_class(*kinds: OntologyEntityKind) -> tuple[str, ...]:
    """The event types that invalidate an assertion over these entity kinds.

    One kind for an entity; a relationship passes both endpoints and gets the
    union, because either end moving makes the edge an as-of claim.
    """
    members: set[str] = set()
    for kind in kinds:
        members.update(event.value for event in _INVALIDATION_CLASSES[kind])
    return tuple(sorted(members))
