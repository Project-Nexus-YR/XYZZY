"""The Meta read vocabulary: what may be asked, and when an answer is still current.

`MetaQuestionKind` is the interface. A caller names the kind it wants and gets
that kind, so every supported question is reachable without guessing a phrasing
and nothing is ever inferred from prose.

Free text is a convenience on top of that, and it is deliberately narrow. Three
passes, in this order, and the order is the design. A question carrying anything
the normalizer cannot fold refuses first, because a normalizer that deletes the
part it cannot read answers a question nobody asked. Then surveillance and
productivity markers refuse, so a question tripping them can never reach a kind.
Then an exact match against a curated corpus of accepted forms. Anything else
refuses. There is no nearest-kind fallback, ever: an ordered table of compiled
patterns was measured against the refusal corpus and made 8 of 11 questions
answer with the *wrong* kind rather than refuse, and answering confidently about
the wrong thing is worse than refusing.

Refusing free text no longer costs a capability, so the corpus stays small and
every accepted form is unambiguous about the one kind it wants. Two forms that
mean opposite things — what is still undecided, and what has been decided — are
two kinds with two queries, never one kind serving both.
"""

from __future__ import annotations

import re
from enum import StrEnum

from .events import EventType
from .models import DomainError, OntologyEntityKind

REFUSAL_PREFIX = "unsupported Meta question"


class MetaQuestionKind(StrEnum):
    """The closed set of questions Meta answers, and the parameter a caller names."""

    STATUS = "STATUS"
    BLOCKERS = "BLOCKERS"
    CHANGES = "CHANGES"
    # Opposite questions, so opposite kinds: an open decision is one still to be
    # taken, a made one is settled. One kind serving both returned the same
    # payload for both and answered the opposite of what was asked.
    DECISIONS_OPEN = "DECISIONS_OPEN"
    DECISIONS_MADE = "DECISIONS_MADE"
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


# Written out rather than inferred, because a rule that guesses which contraction
# was meant is a pattern by another name. Every entry is a fixed substitution.
_CONTRACTIONS = {
    "what's": "what is",
    "where's": "where is",
    "who's": "who is",
    "how's": "how is",
    "that's": "that is",
    "there's": "there is",
    "it's": "it is",
    "we're": "we are",
    "we've": "we have",
    "they're": "they are",
    "i'm": "i am",
    "let's": "let us",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "isn't": "is not",
    "aren't": "are not",
    "hasn't": "has not",
    "haven't": "have not",
    "can't": "cannot",
    "won't": "will not",
}
_TYPOGRAPHIC_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'"})
_NOT_WORD = re.compile(r"[^a-z0-9_']+")
# Everything outside this set is a character the steps below cannot fold — a
# Japanese, Cyrillic or Arabic word, an accented Latin one — and the substitution
# above would silently delete it.
_UNFOLDABLE = re.compile(r"[^\x00-\x7f]")


def normalize_question(question: str) -> str:
    """Fold spelling, so two writings of one question become one key.

    Case, whitespace and ASCII punctuation carry no meaning here, and a contraction
    or a possessive apostrophe is a spelling of the same words. Every step is a
    fixed substitution over ASCII, and `'s` always expands to `is`, so a corpus key
    is a normalized spelling rather than a sentence.

    Dropping punctuation is a loss, so this does not claim never to merge two
    questions — it claims only to lose nothing it cannot read. A character it
    cannot fold refuses here: `status 誰が一番多く働いたか` folded to `status` and
    was answered as a status question, which is a normalizer answering a question
    nobody asked.
    """
    readable = question.lower().translate(_TYPOGRAPHIC_APOSTROPHES)
    if _UNFOLDABLE.search(readable):
        raise DomainError(
            f"{REFUSAL_PREFIX}; it carries characters this workspace cannot read, and a "
            "question it cannot read in full is one it will not answer in part"
        )
    folded = _NOT_WORD.sub(" ", readable)
    expanded = " ".join(_CONTRACTIONS.get(word, word) for word in folded.split())
    return " ".join(expanded.replace("'", "").split())


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
# form cannot silently widen a neighbouring kind, and a form that could belong to
# two kinds belongs here to neither. This set is a convenience, not the interface:
# a caller that names its kind reaches every capability without it, so the set can
# stay small and unambiguous instead of growing towards every phrasing.
ACCEPTED_QUESTIONS: dict[str, MetaQuestionKind] = {
    "status": MetaQuestionKind.STATUS,
    "status update": MetaQuestionKind.STATUS,
    "what is the status": MetaQuestionKind.STATUS,
    "what is the current status": MetaQuestionKind.STATUS,
    "what is the status here": MetaQuestionKind.STATUS,
    "what is the status of this": MetaQuestionKind.STATUS,
    "give me a status update": MetaQuestionKind.STATUS,
    "give me the status": MetaQuestionKind.STATUS,
    "how are things going": MetaQuestionKind.STATUS,
    "how is it going": MetaQuestionKind.STATUS,
    "how is this going": MetaQuestionKind.STATUS,
    "how are we doing": MetaQuestionKind.STATUS,
    "what is going on": MetaQuestionKind.STATUS,
    "what is happening": MetaQuestionKind.STATUS,
    "where do things stand": MetaQuestionKind.STATUS,
    "where does this stand": MetaQuestionKind.STATUS,
    "where do we stand": MetaQuestionKind.STATUS,
    "where are we": MetaQuestionKind.STATUS,
    "where are we at": MetaQuestionKind.STATUS,
    "what is the current state": MetaQuestionKind.STATUS,
    "what is the state of things": MetaQuestionKind.STATUS,
    "summarize the status": MetaQuestionKind.STATUS,
    "catch me up": MetaQuestionKind.STATUS,
    "bring me up to speed": MetaQuestionKind.STATUS,
    "blockers": MetaQuestionKind.BLOCKERS,
    "any blockers": MetaQuestionKind.BLOCKERS,
    "what is blocking": MetaQuestionKind.BLOCKERS,
    "what is blocking us": MetaQuestionKind.BLOCKERS,
    "what is blocking this": MetaQuestionKind.BLOCKERS,
    "what is blocking progress": MetaQuestionKind.BLOCKERS,
    "what is blocked": MetaQuestionKind.BLOCKERS,
    "what is being blocked": MetaQuestionKind.BLOCKERS,
    "what are the blockers": MetaQuestionKind.BLOCKERS,
    "what blockers are there": MetaQuestionKind.BLOCKERS,
    "are there any blockers": MetaQuestionKind.BLOCKERS,
    "is there anything blocking us": MetaQuestionKind.BLOCKERS,
    "is anything blocked": MetaQuestionKind.BLOCKERS,
    "show the blockers": MetaQuestionKind.BLOCKERS,
    "list the blockers": MetaQuestionKind.BLOCKERS,
    "what is in the way": MetaQuestionKind.BLOCKERS,
    "what is stuck": MetaQuestionKind.BLOCKERS,
    "what is holding us up": MetaQuestionKind.BLOCKERS,
    "what is holding this up": MetaQuestionKind.BLOCKERS,
    "what is slowing us down": MetaQuestionKind.BLOCKERS,
    "what needs unblocking": MetaQuestionKind.BLOCKERS,
    "changes": MetaQuestionKind.CHANGES,
    "what changed": MetaQuestionKind.CHANGES,
    "what changed this week": MetaQuestionKind.CHANGES,
    "what changed recently": MetaQuestionKind.CHANGES,
    "what changed lately": MetaQuestionKind.CHANGES,
    "what changed since last week": MetaQuestionKind.CHANGES,
    "what has changed": MetaQuestionKind.CHANGES,
    "what has changed recently": MetaQuestionKind.CHANGES,
    "what has changed lately": MetaQuestionKind.CHANGES,
    # The normalized spelling of "what's changed lately"; `'s` always expands to `is`.
    "what is changed lately": MetaQuestionKind.CHANGES,
    "what is new": MetaQuestionKind.CHANGES,
    "what is different": MetaQuestionKind.CHANGES,
    "what happened recently": MetaQuestionKind.CHANGES,
    "what happened this week": MetaQuestionKind.CHANGES,
    "what moved recently": MetaQuestionKind.CHANGES,
    "any updates": MetaQuestionKind.CHANGES,
    "are there any updates": MetaQuestionKind.CHANGES,
    "what are the updates": MetaQuestionKind.CHANGES,
    "show the changes": MetaQuestionKind.CHANGES,
    "list the changes": MetaQuestionKind.CHANGES,
    # Only forms that say which of the two they want. "decisions", "show the
    # decisions" and "which decisions need review" were dropped: each reads either
    # way, and a form that reads either way is a guess the moment it is answered.
    "any pending decisions": MetaQuestionKind.DECISIONS_OPEN,
    "what decisions are pending": MetaQuestionKind.DECISIONS_OPEN,
    "which decisions are pending": MetaQuestionKind.DECISIONS_OPEN,
    "what decisions are open": MetaQuestionKind.DECISIONS_OPEN,
    "what are the open decisions": MetaQuestionKind.DECISIONS_OPEN,
    "what decisions are waiting": MetaQuestionKind.DECISIONS_OPEN,
    "what do we need to decide": MetaQuestionKind.DECISIONS_OPEN,
    "what needs deciding": MetaQuestionKind.DECISIONS_OPEN,
    "what needs to be decided": MetaQuestionKind.DECISIONS_OPEN,
    "what still needs a decision": MetaQuestionKind.DECISIONS_OPEN,
    "what is undecided": MetaQuestionKind.DECISIONS_OPEN,
    "what decisions have been made": MetaQuestionKind.DECISIONS_MADE,
    "what has been decided": MetaQuestionKind.DECISIONS_MADE,
    "what has already been decided": MetaQuestionKind.DECISIONS_MADE,
    "what did we decide": MetaQuestionKind.DECISIONS_MADE,
    "what decisions were made": MetaQuestionKind.DECISIONS_MADE,
    "disagreement": MetaQuestionKind.DISAGREEMENT,
    "any disagreement": MetaQuestionKind.DISAGREEMENT,
    "is there any disagreement": MetaQuestionKind.DISAGREEMENT,
    "where is the disagreement": MetaQuestionKind.DISAGREEMENT,
    "what is the disagreement": MetaQuestionKind.DISAGREEMENT,
    "where is there disagreement": MetaQuestionKind.DISAGREEMENT,
    "what are we disagreeing about": MetaQuestionKind.DISAGREEMENT,
    "where do we disagree": MetaQuestionKind.DISAGREEMENT,
    "where do the agents disagree": MetaQuestionKind.DISAGREEMENT,
    "what do the agents disagree about": MetaQuestionKind.DISAGREEMENT,
    "what is contested": MetaQuestionKind.DISAGREEMENT,
    "what is disputed": MetaQuestionKind.DISAGREEMENT,
    "what is in dispute": MetaQuestionKind.DISAGREEMENT,
    "what is contradicted": MetaQuestionKind.DISAGREEMENT,
    "what are the contradictions": MetaQuestionKind.DISAGREEMENT,
    "where are the contradictions": MetaQuestionKind.DISAGREEMENT,
    "what conflicts are there": MetaQuestionKind.DISAGREEMENT,
    "where is the conflict": MetaQuestionKind.DISAGREEMENT,
    "what is being argued about": MetaQuestionKind.DISAGREEMENT,
    "show the disagreement": MetaQuestionKind.DISAGREEMENT,
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
    """Refuse the unreadable first, refuse the surveillance marker second, match exactly third.

    Only for free text. A caller holding a `MetaQuestionKind` never comes through
    here, so a refusal costs a phrasing rather than a capability.
    """
    normalized = normalize_question(question)
    if _bears_surveillance_marker(normalized):
        raise DomainError(
            f"{REFUSAL_PREFIX}; this workspace does not answer for individual activity, "
            "output volume, ranking or productivity"
        )
    kind = ACCEPTED_QUESTIONS.get(normalized)
    if kind is None:
        # What Meta answers, so the asker can rephrase instead of guessing — the
        # subjects, never the accepted forms, which would publish the corpus.
        raise DomainError(
            f"{REFUSAL_PREFIX}; Meta answers where things stand, what is blocked, "
            "what changed, which decisions are still open, which decisions have been "
            "made, where the disagreement is, why a decision was made, and what "
            "evidence supports it — ask for one of those in ordinary words, or name "
            "the kind outright"
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
        # A publication emits one of two types, chosen by synthesis type, and the
        # Decision Brief is the one that re-decides. Both belong to the class.
        EventType.DECISION_BRIEF_SYNTHESIZED,
        EventType.SYNTHESIS_PUBLISHED,
        *_SUPERSEDED,
    ),
    OntologyEntityKind.ARTIFACT: (
        EventType.ARTIFACT_CREATED,
        EventType.ARTIFACT_UPDATED,
        EventType.ARTIFACT_VERSION_CREATED,
        EventType.DECISION_BRIEF_SYNTHESIZED,
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
