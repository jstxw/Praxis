"""Mutation proposal (DESIGN §4).

Four mutation types in build priority order — memory, verification
policy, skills, instructions (lowest; instruction-only evolution
regresses in the published ablation) — plus **deletion**, the only
defense against harness rot.

Every proposal carries a falsifiable contract: ``predicted_fix`` (tasks
it should fix) and ``predicted_risk`` (tasks it might break), recorded
now and scored on the next cycle. Attribution is known to be reliable
for fixes and near-random for regressions, so ``predicted_risk`` is
recorded to be scored later, not trusted now — the regression suite is
not optional.

Branch factor 3: for each lesson, up to three candidates that attack
the same pattern through *different* components, so the comparison also
tells us which surface carries the fix.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.continual.harness_object import (
    HarnessSnapshot,
    Instruction,
    MemoryEntry,
    Skill,
    VerificationRule,
)
from app.refinement.reflection import FailurePattern

BRANCH_FACTOR = 3
COMPONENT_PRIORITY = {"memory": 0, "verification": 1, "skill": 2, "instruction": 3, "control": 4}


@dataclass
class MutationProposal:
    component: str  # memory | verification | skill | instruction | control
    op: str  # add | delete
    hypothesis: str
    predicted_fix: list[str]
    predicted_risk: list[str]
    add_rules: list[VerificationRule] = field(default_factory=list)
    add_skills: list[Skill] = field(default_factory=list)
    add_instructions: list[Instruction] = field(default_factory=list)
    add_memory: list[MemoryEntry] = field(default_factory=list)
    remove_names: list[str] = field(default_factory=list)  # rules/skills/instructions
    remove_memory_ids: list[str] = field(default_factory=list)
    pattern_name: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def payload(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "op": self.op,
            "add_rules": [r.model_dump(mode="json") for r in self.add_rules],
            "add_skills": [s.model_dump(mode="json") for s in self.add_skills],
            "add_instructions": [i.model_dump(mode="json") for i in self.add_instructions],
            "add_memory": [m.model_dump(mode="json") for m in self.add_memory],
            "remove_names": self.remove_names,
            "remove_memory_ids": self.remove_memory_ids,
            "pattern": self.pattern_name,
        }

    @property
    def touches_memory(self) -> bool:
        return bool(self.add_memory or self.remove_memory_ids)


def apply_to_snapshot(snapshot: HarnessSnapshot, proposal: MutationProposal) -> HarnessSnapshot:
    """New (I, S, V, C); the parent snapshot is not modified."""
    child = HarnessSnapshot.from_json(snapshot.to_json())
    removed = set(proposal.remove_names)
    child.verification = [r for r in child.verification if r.name not in removed]
    child.skills = [s for s in child.skills if s.name not in removed]
    child.instructions = [i for i in child.instructions if i.name not in removed]
    existing = {r.name for r in child.verification} | {s.name for s in child.skills} | {
        i.name for i in child.instructions
    }
    child.verification += [r for r in proposal.add_rules if r.name not in existing]
    child.skills += [s for s in proposal.add_skills if s.name not in existing]
    child.instructions += [i for i in proposal.add_instructions if i.name not in existing]
    return child


def _risk(pattern: FailurePattern, candidate_tasks: list[str], limit: int = 5) -> list[str]:
    """Tasks the change will also touch but where the pattern never showed."""
    return [t for t in candidate_tasks if t not in pattern.task_ids][:limit]


def propose_for_pattern(
    pattern: FailurePattern,
    parent: HarnessSnapshot,
    *,
    tasks: dict[str, Any] | None = None,
    risk_pool: list[str] | None = None,
    branch_factor: int = BRANCH_FACTOR,
) -> list[MutationProposal]:
    """Template proposals for a recognized pattern, highest priority first."""
    fix = list(pattern.task_ids)
    risk = _risk(pattern, risk_pool or [])
    have = {r.name for r in parent.verification} | {s.name for s in parent.skills} | {
        i.name for i in parent.instructions
    }
    out: list[MutationProposal] = []

    def add(p: MutationProposal) -> None:
        names = [r.name for r in p.add_rules] + [s.name for s in p.add_skills] + [
            i.name for i in p.add_instructions
        ]
        if names and all(n in have for n in names):
            return  # already in the harness: not a mutation
        p.pattern_name = pattern.name
        out.append(p)

    name = pattern.name
    if name == "redundant_full_suite":
        add(MutationProposal(
            component="verification", op="add",
            hypothesis="A targeted-tests verification rule stops full-suite reruns "
                       "and cuts tool calls without losing success.",
            predicted_fix=fix, predicted_risk=risk,
            add_rules=[VerificationRule(
                name="targeted_tests", kind="targeted_tests",
                description="Verify with the task's own test file; do not run the "
                            "full suite unless you changed shared code.")],
        ))
        add(MutationProposal(
            component="skill", op="add",
            hypothesis="A targeted-testing skill triggered on test work has the same "
                       "effect with a narrower injection footprint.",
            predicted_fix=fix, predicted_risk=risk,
            add_skills=[Skill(
                name="targeted_testing", description="Run only the tests covering your change.",
                triggers=["test", "tests", "pytest"],
                steps=["identify the test file for the code you changed",
                       "run only that file", "run the full suite only if shared code changed"])],
        ))
        add(MutationProposal(
            component="instruction", op="add",
            hypothesis="An instruction overlay asking for targeted tests (lowest-priority "
                       "surface; expected weaker).",
            predicted_fix=fix, predicted_risk=risk,
            add_instructions=[Instruction(
                name="prefer_targeted_tests",
                text="Only the task's test file needs to pass; not the full suite.")],
        ))
    elif name == "redundant_reads":
        add(MutationProposal(
            component="skill", op="add",
            hypothesis="A read-once skill removes re-reads of unchanged files.",
            predicted_fix=fix, predicted_risk=risk,
            add_skills=[Skill(
                name="read_once", description="Read each file once; do not re-read a file "
                                              "you have not changed since you last read it.",
                always=True, steps=["keep notes of what each file contains"])],
        ))
        add(MutationProposal(
            component="instruction", op="add",
            hypothesis="An instruction against re-reading unchanged files.",
            predicted_fix=fix, predicted_risk=risk,
            add_instructions=[Instruction(
                name="no_reread",
                text="You already read a file once; do not re-read it unless it changed.")],
        ))
    elif name in {"blind_retry"}:
        add(MutationProposal(
            component="verification", op="add",
            hypothesis="Requiring inspection of the failure before re-editing raises the "
                       "retry fix rate and removes wasted attempts.",
            predicted_fix=fix, predicted_risk=risk,
            add_rules=[VerificationRule(
                name="inspect_failure", kind="inspect_failure",
                description="After a failing run, read the failure output and the code "
                            "before editing again (no blind retries).")],
        ))
        add(MutationProposal(
            component="skill", op="add",
            hypothesis="A debugging skill triggered on fix tasks has the same effect.",
            predicted_fix=fix, predicted_risk=risk,
            add_skills=[Skill(
                name="debug_failures", description="Inspect the failure before retrying.",
                triggers=["fix", "bug", "error", "handle"],
                steps=["read the failing assertion", "read the code it exercises",
                       "only then edit — read the failure before editing again"])],
        ))
    elif name == "unverified_submit":
        add(MutationProposal(
            component="verification", op="add",
            hypothesis="Requiring a test run before submit removes untested wrong fixes.",
            predicted_fix=fix, predicted_risk=risk,
            add_rules=[VerificationRule(
                name="run_tests_before_submit", kind="run_tests",
                description="Run the task's tests before submitting; verify before submit.")],
        ))
        add(MutationProposal(
            component="instruction", op="add",
            hypothesis="An instruction to verify before submitting (weaker surface).",
            predicted_fix=fix, predicted_risk=risk,
            add_instructions=[Instruction(
                name="verify_before_submit", text="Run tests before submit.")],
        ))
    elif name == "test_tampering":
        add(MutationProposal(
            component="verification", op="add",
            hypothesis="An explicit no-test-edits rule removes voided runs.",
            predicted_fix=fix, predicted_risk=risk,
            add_rules=[VerificationRule(
                name="no_test_edits", kind="no_test_edits",
                description="Never modify existing tests; fix the code under test.")],
        ))
    elif name == "exploration_waste":
        entries = []
        for task_id in pattern.task_ids:
            task = (tasks or {}).get(task_id)
            if task is None or not task.scope_files:
                continue
            scope = task.scope_files[0]
            entries.append(MemoryEntry(
                kind="repo_fact",
                text=f"For '{task.instruction[:60]}…' the relevant code is in {scope} "
                     f"(tests: {', '.join(task.test_files)}).",
                keys=[scope, *task.test_files, Path(scope).stem],
                source=f"pattern:{pattern.name}",
            ))
        if entries:
            add(MutationProposal(
                component="memory", op="add",
                hypothesis="Repository facts locating the code in scope remove exploratory "
                           "reads (global: acts from step 0).",
                predicted_fix=fix, predicted_risk=risk, add_memory=entries,
            ))
        add(MutationProposal(
            component="skill", op="add",
            hypothesis="A locate-first skill (grep for the named symbol, open that file) "
                       "reduces unrelated reads without repo-specific memory.",
            predicted_fix=fix, predicted_risk=risk,
            add_skills=[Skill(
                name="locate_first", description="Find the file named in the task before "
                                                 "opening anything else.",
                always=True, steps=["open the file the task names first"])],
        ))
    out.sort(key=lambda p: COMPONENT_PRIORITY[p.component])
    return out[:branch_factor]


def propose_deletions(
    parent: HarnessSnapshot,
    *,
    injected_counts: dict[str, int],
    n_recent: int,
    memory: list[MemoryEntry] | None = None,
    retrieved_counts: dict[str, int] | None = None,
    existing_paths: set[str] | None = None,
) -> list[MutationProposal]:
    """Harness-rot defense: remove dead or stale content.

    - skills never injected across ``n_recent`` trajectories are dead weight;
    - memory facts whose keyed paths no longer exist are confidently
      wrong, which is worse than no fact.
    """
    proposals: list[MutationProposal] = []
    if n_recent >= 10:
        dead = [s.name for s in parent.skills if injected_counts.get(s.name, 0) == 0]
        if dead:
            proposals.append(MutationProposal(
                component="skill", op="delete",
                hypothesis=f"Skills {dead} were never triggered in {n_recent} tasks; "
                           "removing them costs nothing and shrinks the harness.",
                predicted_fix=[], predicted_risk=[], remove_names=dead,
            ))
    if memory is not None and existing_paths is not None:
        stale = [
            m.id for m in memory
            if any("/" in k or k.endswith(".py") for k in m.keys)
            and not any(k in existing_paths for k in m.keys if "/" in k or k.endswith(".py"))
        ]
        if stale:
            proposals.append(MutationProposal(
                component="memory", op="delete",
                hypothesis=f"{len(stale)} memory facts reference files that no longer exist.",
                predicted_fix=[], predicted_risk=[], remove_memory_ids=stale,
            ))
    return proposals


def propose(
    lessons: list[FailurePattern],
    parent: HarnessSnapshot,
    *,
    tasks: dict[str, Any] | None = None,
    risk_pool: list[str] | None = None,
    deletions: list[MutationProposal] | None = None,
    branch_factor: int = BRANCH_FACTOR,
) -> list[tuple[FailurePattern | None, MutationProposal]]:
    """Candidates for the top lesson (plus any deletions), capped at the
    branch factor. One lesson per cycle keeps attribution clean."""
    out: list[tuple[FailurePattern | None, MutationProposal]] = []
    for pattern in lessons:
        proposals = propose_for_pattern(
            pattern, parent, tasks=tasks, risk_pool=risk_pool, branch_factor=branch_factor
        )
        if proposals:
            out = [(pattern, p) for p in proposals]
            break
    for deletion in deletions or []:
        if len(out) >= branch_factor:
            break
        out.append((None, deletion))
    return out
