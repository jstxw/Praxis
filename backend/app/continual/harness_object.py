"""The harness object ``H = (I, S, M, V, C)`` (ARCHITECTURE §2).

| Component | Contents | Priority when evolving |
|---|---|---|
| ``I`` instructions  | prompt overlays                          | lowest |
| ``S`` skills        | declarative procedures with triggers     |        |
| ``M`` memory        | repo facts, behavioral, procedural       | separately versioned |
| ``V`` verification  | what must pass before submit             | highest |
| ``C`` control       | retry caps, read windows, budgets        |        |

:class:`HarnessSnapshot` is ``(I, S, V, C)`` — deliberately *without*
memory, which lives in :class:`MemoryVersion` and is pinned separately
so a comparison never attributes accumulated repo knowledge to a
mutation.

Context injection (ARCHITECTURE §8) is retrieve-and-rank, never "inject
the whole harness": top-k memory, triggered skills only, the
verification policy, under a hard token budget. The injected size is
returned so it can be tracked as a metric — context spent on the
harness is context taken from the task.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

MemoryKind = Literal["repo_fact", "behavioral", "procedural"]
Component = Literal["memory", "verification", "skill", "instruction", "control"]

COMPONENTS: tuple[str, ...] = ("memory", "verification", "skill", "instruction", "control")
DEFAULT_CONTEXT_BUDGET_TOKENS = 2000


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (~4 chars/token).

    Used for budgets and complexity, never billed against; real token
    counts come from the agent's own usage reports.
    """
    return (len(text) + 3) // 4


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*")


def tokenize(text: str) -> set[str]:
    """Lowercased identifier-ish tokens; paths are also split on / and .."""
    tokens: set[str] = set()
    for match in _WORD.findall(text.lower()):
        tokens.add(match)
        for part in re.split(r"[./-]", match):
            if len(part) >= 3:
                tokens.add(part)
    return tokens


# ─────────────────────────────────────────────────────────────────────
# Components
# ─────────────────────────────────────────────────────────────────────


class MemoryEntry(BaseModel):
    """One memory item. ``keys`` drive retrieval (paths, identifiers)."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: MemoryKind = "repo_fact"
    text: str
    keys: list[str] = Field(default_factory=list)
    source: str | None = None  # trajectory / pattern id that produced it


class Skill(BaseModel):
    """A declarative procedure. Triggered only when a trigger matches.

    ``triggers`` are lowercase keywords matched against the task text;
    ``always`` skills are injected unconditionally (use sparingly — they
    are pure context cost).
    """

    name: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    always: bool = False
    steps: list[str] = Field(default_factory=list)


class VerificationRule(BaseModel):
    """What must hold before submit.

    ``kind`` is a closed vocabulary so both the agent-facing rendering
    and the evaluator-side audit can interpret it:

    - ``run_tests``        — run the task's tests before submitting
    - ``targeted_tests``   — run only the tests that cover changed code,
                             not the full suite
    - ``no_test_edits``    — never modify existing tests
    - ``inspect_failure``  — after a failing run, read the failure and the
                             code before editing again (no blind retry)
    - ``diff_cap``         — keep the change under ``params.max_lines``
    - ``custom``           — free text only (rendered, never enforced)
    """

    name: str
    kind: Literal[
        "run_tests", "targeted_tests", "no_test_edits", "inspect_failure",
        "diff_cap", "custom",
    ]
    description: str
    params: dict[str, Any] = Field(default_factory=dict)


class Instruction(BaseModel):
    """A prompt overlay. Lowest evolution priority (AHE ablation)."""

    name: str
    text: str


class Control(BaseModel):
    """Retry caps, read windows, budgets."""

    max_verify_retries: int = 3
    max_tool_calls: int = 60
    read_window_lines: int = 400
    context_budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS


class HarnessSnapshot(BaseModel):
    """``(I, S, V, C)`` — immutable content of one harness version."""

    instructions: list[Instruction] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    verification: list[VerificationRule] = Field(default_factory=list)
    control: Control = Field(default_factory=Control)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "HarnessSnapshot":
        return cls.model_validate(data)

    def digest(self) -> str:
        return content_hash(self.to_json())

    def complexity(self) -> dict[str, int]:
        """The complexity term of ``Score' = Score − λ·Complexity(H)``."""
        full_text = "\n".join(
            [i.text for i in self.instructions]
            + [_render_skill(s) for s in self.skills]
            + [_render_rule(r) for r in self.verification]
        )
        return {
            "prompt_tokens": estimate_tokens(full_text),
            "instruction_count": len(self.instructions),
            "skill_count": len(self.skills),
            "policy_count": len(self.verification),
            "harness_bytes": len(canonical_json(self.to_json()).encode()),
        }

    def rule_kinds(self) -> set[str]:
        return {r.kind for r in self.verification}


def complexity_score(complexity: dict[str, int]) -> float:
    """Scalar complexity: prompt tokens per 100, plus one per item."""
    return (
        complexity.get("prompt_tokens", 0) / 100.0
        + complexity.get("instruction_count", 0)
        + complexity.get("skill_count", 0)
        + complexity.get("policy_count", 0)
    )


def empty_harness() -> HarnessSnapshot:
    """H0 for experiments: nothing but default control."""
    return HarnessSnapshot()


# ─────────────────────────────────────────────────────────────────────
# Context injection
# ─────────────────────────────────────────────────────────────────────


@dataclass
class RenderedContext:
    """The ``<HARNESS_CONTEXT>`` block plus what went into it."""

    text: str
    tokens: int
    memory_ids: list[str] = field(default_factory=list)
    skill_names: list[str] = field(default_factory=list)
    rule_names: list[str] = field(default_factory=list)
    instruction_names: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # cut by the budget

    def to_json(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "memory_ids": self.memory_ids,
            "skill_names": self.skill_names,
            "rule_names": self.rule_names,
            "instruction_names": self.instruction_names,
            "dropped": self.dropped,
        }


def _render_skill(skill: Skill) -> str:
    steps = "; ".join(skill.steps)
    return f"{skill.name}: {skill.description}" + (f" Steps: {steps}" if steps else "")


def _render_rule(rule: VerificationRule) -> str:
    return f"[{rule.kind}] {rule.description}"


def rank_memory(
    entries: Iterable[MemoryEntry], query_tokens: set[str]
) -> list[tuple[float, MemoryEntry]]:
    """Score memory entries by overlap with the query; keys weigh 3×."""
    scored: list[tuple[float, MemoryEntry]] = []
    for entry in entries:
        key_tokens: set[str] = set()
        for key in entry.keys:
            key_tokens |= tokenize(key)
        text_tokens = tokenize(entry.text)
        score = 3.0 * len(key_tokens & query_tokens) + len(text_tokens & query_tokens)
        if score > 0:
            scored.append((score, entry))
    # Stable: score desc, then id for determinism.
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return scored


def triggered_skills(skills: Iterable[Skill], query_tokens: set[str]) -> list[Skill]:
    out = []
    for skill in skills:
        if skill.always or any(t.lower() in query_tokens for t in skill.triggers):
            out.append(skill)
    return out


def render_context(
    snapshot: HarnessSnapshot,
    memory: Iterable[MemoryEntry],
    *,
    task_text: str,
    top_k_memory: int = 8,
) -> RenderedContext:
    """Retrieve and rank; never inject the whole harness.

    Priority under the budget mirrors build priority: verification
    first, then memory, skills, and instructions last. Items that do not
    fit are recorded in ``dropped``.
    """
    budget = snapshot.control.context_budget_tokens
    query = tokenize(task_text)

    rules = list(snapshot.verification)
    memories = [e for _, e in rank_memory(memory, query)[:top_k_memory]]
    skills = triggered_skills(snapshot.skills, query)
    instructions = list(snapshot.instructions)

    header, footer = "<HARNESS_CONTEXT>", "</HARNESS_CONTEXT>"
    used = estimate_tokens(header + footer) + 12  # section labels
    ctx = RenderedContext(text="", tokens=0)
    sections: dict[str, list[str]] = {
        "Verification": [], "Repository facts": [], "Active skills": [],
        "Instructions": [],
    }

    def admit(section: str, line: str, label: str, sink: list[str]) -> None:
        nonlocal used
        cost = estimate_tokens(line) + 1
        if used + cost > budget:
            ctx.dropped.append(label)
            return
        used += cost
        sections[section].append(line)
        sink.append(label)

    for rule in rules:
        admit("Verification", _render_rule(rule), rule.name, ctx.rule_names)
    for entry in memories:
        admit("Repository facts", entry.text, entry.id, ctx.memory_ids)
    for skill in skills:
        admit("Active skills", _render_skill(skill), skill.name, ctx.skill_names)
    for instruction in instructions:
        admit("Instructions", instruction.text, instruction.name, ctx.instruction_names)

    if not any(sections.values()):
        return ctx  # nothing to inject: empty text, zero tokens

    lines = [header]
    for title in ("Repository facts", "Active skills", "Verification", "Instructions"):
        if sections[title]:
            lines.append(f"{title}:")
            lines.extend(f"- {item}" for item in sections[title])
    lines.append(footer)
    ctx.text = "\n".join(lines)
    ctx.tokens = estimate_tokens(ctx.text)
    return ctx
