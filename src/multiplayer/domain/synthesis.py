"""The synthesis types of PRD §8 and what distinguishes them.

All three answer the same question differently: a General Synthesis reconciles what the
selected outputs say, a Decision Brief recommends an action, a Progress Report states where
the work stands. What they share is the part that must not vary — a summary and claims, each
citing the exact AgentOutputs it came from — so provenance is identical across types and only
the surrounding sections change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SynthesisType(StrEnum):
    GENERAL_SYNTHESIS = "GENERAL_SYNTHESIS"
    DECISION_BRIEF = "DECISION_BRIEF"
    PROGRESS_REPORT = "PROGRESS_REPORT"


@dataclass(frozen=True, slots=True)
class Section:
    """One rendered section of a synthesis document, beyond the shared claims."""

    heading: str
    key: str
    is_list: bool
    unavailable: str
    """What the section says when no model produced it, so the gap is stated, not blank."""


@dataclass(frozen=True, slots=True)
class SynthesisSpec:
    type: SynthesisType
    artifact_name: str
    instruction: str
    before_claims: tuple[Section, ...]
    after_claims: tuple[Section, ...]


SPECS: dict[SynthesisType, SynthesisSpec] = {
    SynthesisType.GENERAL_SYNTHESIS: SynthesisSpec(
        type=SynthesisType.GENERAL_SYNTHESIS,
        artifact_name="General Synthesis",
        instruction=(
            "Return a general synthesis as JSON: reconcile what the selected outputs "
            "establish, name the themes they share, and leave open what they do not settle. "
            "Do not recommend an action."
        ),
        before_claims=(),
        after_claims=(
            Section("Themes", "themes", True, "No themes were identified."),
            Section(
                "Open questions",
                "open_questions",
                True,
                "No open questions were identified.",
            ),
        ),
    ),
    SynthesisType.DECISION_BRIEF: SynthesisSpec(
        type=SynthesisType.DECISION_BRIEF,
        artifact_name="Decision Brief",
        instruction=(
            "Return a concise decision brief as JSON. Every claim must cite one or more exact "
            "source_output_ids from the supplied identifiers; never invent an identifier."
        ),
        before_claims=(
            Section(
                "Recommendation [AI-derived]",
                "recommendation",
                False,
                "No decision recommendation was generated.",
            ),
        ),
        after_claims=(
            Section("Risks", "risks", True, "This is not model-generated analysis."),
            Section(
                "Uncertainties",
                "uncertainties",
                True,
                "All substantive decision analysis remains unperformed.",
            ),
            Section(
                "Next action",
                "next_action",
                False,
                "Configure a model provider and request a new synthesis.",
            ),
        ),
    ),
    SynthesisType.PROGRESS_REPORT: SynthesisSpec(
        type=SynthesisType.PROGRESS_REPORT,
        artifact_name="Progress Report",
        instruction=(
            "Return a progress report as JSON: state where the work stands, what is finished, "
            "what is still moving, and what is blocked. Report only what the selected outputs "
            "evidence; do not estimate progress they do not state."
        ),
        before_claims=(Section("Status", "status", False, "No progress status was generated."),),
        after_claims=(
            Section("Completed", "completed", True, "No completed work was reported."),
            Section("In flight", "in_flight", True, "No work in flight was reported."),
            Section("Blocked", "blocked", True, "No blockers were reported."),
            Section(
                "Next step",
                "next_step",
                False,
                "Configure a model provider and request a new synthesis.",
            ),
        ),
    ),
}


RESERVED_ARTIFACT_NAMES = frozenset(spec.artifact_name for spec in SPECS.values())
"""Names a published synthesis owns; nothing hand-written may claim one."""


def spec_for(synthesis_type: str) -> SynthesisSpec:
    """Resolve a stored type string. Unknown types are a domain error, not a default."""
    try:
        return SPECS[SynthesisType(synthesis_type)]
    except ValueError as exc:
        raise ValueError(f"unknown synthesis type: {synthesis_type}") from exc


def document_schema(spec: SynthesisSpec) -> dict[str, Any]:
    """The JSON contract the provider must satisfy for this type, in document order."""
    claims_schema: dict[str, Any] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "source_output_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["text", "source_output_ids", "confidence"],
            "additionalProperties": False,
        },
        "minItems": 1,
    }
    properties: dict[str, Any] = {"summary": {"type": "string"}}
    for section in spec.before_claims:
        properties[section.key] = _section_schema(section)
    properties["claims"] = claims_schema
    for section in spec.after_claims:
        properties[section.key] = _section_schema(section)
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _section_schema(section: Section) -> dict[str, Any]:
    if section.is_list:
        return {"type": "array", "items": {"type": "string"}}
    return {"type": "string"}


def unavailable_sections(spec: SynthesisSpec) -> dict[str, Any]:
    """Section values for a document no model produced, so nothing renders blank."""
    return {
        section.key: [section.unavailable] if section.is_list else section.unavailable
        for section in spec.before_claims + spec.after_claims
    }


def render(spec: SynthesisSpec, title: str, document: dict[str, Any], simulated: bool) -> str:
    """Render the published document. Claims always carry their sources and confidence."""
    marker = " [SIMULATED SYNTHESIS]" if simulated else ""
    lines = [f"# {title}{marker}", "", str(document.get("summary", "")).strip(), ""]
    for section in spec.before_claims:
        lines.extend(_section_lines(section, document))
    lines.append("## Claims")
    claims = document.get("claims")
    if isinstance(claims, list):
        for ordinal, claim in enumerate(claims, start=1):
            if isinstance(claim, dict):
                source_ids = ", ".join(f"`{item}`" for item in claim["source_output_ids"])
                lines.extend(
                    [
                        f"### Claim {ordinal} [AI-derived]",
                        str(claim["text"]),
                        f"Sources: {source_ids}",
                        f"Confidence: {float(claim['confidence']):.2f}",
                        "",
                    ]
                )
    for section in spec.after_claims:
        lines.extend(_section_lines(section, document))
    return "\n".join(lines).rstrip() + "\n"


def _section_lines(section: Section, document: dict[str, Any]) -> list[str]:
    if section.is_list:
        values = document.get(section.key)
        items = [f"- {value}" for value in values] if isinstance(values, list) else []
        return [f"## {section.heading}", *items, ""]
    return [f"## {section.heading}", str(document.get(section.key, "")).strip(), ""]
