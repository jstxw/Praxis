"""Reflection — what failed, which component, and **at which step** (DESIGN §3).

Output::

    class ReflectionReport(BaseModel):
        strengths:        list[str]
        failure_patterns: list[FailurePattern]
        should_evolve:    bool

Each pattern carries, per supporting trajectory, the divergence step:
the event sequence number where the pattern first manifests. Without it
there is no fork point and evaluation falls back to from-scratch runs.

Two reflectors share this contract:

- :class:`HeuristicReflector` — deterministic detectors over the
  six-tool trajectory vocabulary. Cheap, reproducible, auditable; its
  false-positive rate is measurable against hand labels (E3).
- :class:`LLMReflector` — the prompt shape from DESIGN §3, run through
  an injected ``runner`` (the ``claude`` CLI or the Anthropic API). Its
  output is *validated*: patterns whose divergence steps don't exist in
  the cited trajectories are dropped, never trusted.

Failure classification precedes any proposal. Environmental failures,
model randomness, task ambiguity and repository issues are not lessons;
only harness deficiencies become mutations.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents.base import TOOL_BASH, TOOL_COMPLETE, TOOL_PATCH, TOOL_READ, TOOL_WRITE
from app.continual.experience import (
    EvaluationRecord,
    StepRecord,
    TrajectoryRecord,
)

Classification = Literal[
    "harness_deficiency",
    "environmental",
    "model_randomness",
    "task_ambiguity",
    "repository_issue",
]
MIN_OCCURRENCES = 3


class FailurePattern(BaseModel):
    name: str
    description: str
    trajectory_ids: list[str]
    divergence_steps: list[int]
    task_ids: list[str] = Field(default_factory=list)
    affected_component: Literal["memory", "verification", "skill", "instruction"]
    scope: Literal["local", "global"]
    classification: Classification = "harness_deficiency"
    evidence: list[str] = Field(default_factory=list)

    @property
    def occurrence_count(self) -> int:
        return len(self.trajectory_ids)

    def fork_points(self) -> list[tuple[str, int]]:
        """``(trajectory, d−1)`` per occurrence — where candidates fork."""
        return [(t, max(0, d - 1)) for t, d in zip(self.trajectory_ids, self.divergence_steps)]


class ReflectionReport(BaseModel):
    strengths: list[str] = Field(default_factory=list)
    failure_patterns: list[FailurePattern] = Field(default_factory=list)
    should_evolve: bool = False
    reflector: str = "heuristic"
    discarded: list[str] = Field(default_factory=list)  # patterns classified away

    def lessons(self) -> list[FailurePattern]:
        return [p for p in self.failure_patterns if p.classification == "harness_deficiency"]


@dataclass
class TrajectoryView:
    """What a reflector sees for one trajectory."""

    trajectory: TrajectoryRecord
    evaluation: EvaluationRecord
    steps: list[StepRecord]
    task: Any | None  # TaskSpec when known (corpus); None for project tasks


# ─────────────────────────────────────────────────────────────────────
# Detectors — each returns the first divergence step or None
# ─────────────────────────────────────────────────────────────────────


def _tool_steps(steps: Iterable[StepRecord]) -> list[StepRecord]:
    return [s for s in steps if s.kind == "tool_call"]


def _is_test_run(step: StepRecord) -> bool:
    return step.tool == TOOL_BASH and "pytest" in str(step.input.get("command", ""))


def _paths(step: StepRecord) -> set[str]:
    return {p for p in str(step.input.get("path", "")).split(",") if p}


def _is_test_file(path: str, task: Any | None) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        (task is not None and path in getattr(task, "test_files", ()))
        or path.startswith("tests/")
        or name.startswith("test_")
        or name == "conftest.py"
    )


def detect_redundant_full_suite(steps: list[StepRecord], task: Any | None) -> int | None:
    """Full-suite test runs when a targeted command exists."""
    if task is None or task.full_test_command == task.test_command:
        return None
    for step in _tool_steps(steps):
        if not _is_test_run(step):
            continue
        command = str(step.input.get("command", ""))
        targeted = any(tf in command for tf in task.test_files)
        if not targeted:
            return step.step
    return None


def detect_redundant_reads(steps: list[StepRecord], task: Any | None) -> int | None:
    seen: set[str] = set()
    for step in _tool_steps(steps):
        if step.tool == TOOL_READ:
            path = str(step.input.get("path", ""))
            if path in seen:
                return step.step
            seen.add(path)
        elif step.tool in {TOOL_PATCH, TOOL_WRITE}:
            seen -= _paths(step)
    return None


def detect_blind_retry(steps: list[StepRecord], task: Any | None) -> int | None:
    """An edit right after a failing test run, with no read in between."""
    failed_run = False
    for step in _tool_steps(steps):
        if _is_test_run(step):
            failed_run = step.is_error
            continue
        if step.tool == TOOL_READ:
            failed_run = False
            continue
        if step.tool in {TOOL_PATCH, TOOL_WRITE} and failed_run:
            return step.step
    return None


def detect_unverified_submit(steps: list[StepRecord], task: Any | None) -> int | None:
    """Submitting with no test run since the last edit."""
    tested_since_edit = True
    for step in _tool_steps(steps):
        if step.tool in {TOOL_PATCH, TOOL_WRITE}:
            tested_since_edit = False
        elif _is_test_run(step):
            tested_since_edit = True
        elif step.tool == TOOL_COMPLETE and not tested_since_edit:
            return step.step
    return None


def detect_test_tampering(steps: list[StepRecord], task: Any | None) -> int | None:
    for step in _tool_steps(steps):
        if step.tool in {TOOL_PATCH, TOOL_WRITE} and any(
            _is_test_file(p, task) for p in _paths(step)
        ):
            return step.step
    return None


def detect_exploration_waste(steps: list[StepRecord], task: Any | None) -> int | None:
    """Reading unrelated files before the first in-scope read."""
    if task is None or not task.scope_files:
        return None
    for step in _tool_steps(steps):
        if step.tool != TOOL_READ:
            continue
        path = str(step.input.get("path", ""))
        if path in task.scope_files or _is_test_file(path, task):
            return None
        return step.step
    return None


@dataclass(frozen=True)
class Detector:
    name: str
    description: str
    component: str
    scope: str
    fn: Callable[[list[StepRecord], Any | None], int | None]
    failures_only: bool  # a lesson only when the run failed


DETECTORS: tuple[Detector, ...] = (
    Detector("redundant_full_suite",
             "ran the full test suite although a targeted test command exists",
             "verification", "local", detect_redundant_full_suite, False),
    Detector("redundant_reads",
             "re-read a file that had not changed since it was last read",
             "skill", "local", detect_redundant_reads, False),
    Detector("blind_retry",
             "edited again immediately after a failing test run without reading "
             "the failure or the code",
             "verification", "local", detect_blind_retry, True),
    Detector("unverified_submit",
             "submitted without running tests after the last edit",
             "verification", "local", detect_unverified_submit, True),
    Detector("test_tampering", "modified test files instead of the code under test",
             "verification", "local", detect_test_tampering, False),
    Detector("exploration_waste",
             "read unrelated files before locating the code in scope",
             "memory", "global", detect_exploration_waste, False),
)
DETECTOR_BY_NAME = {d.name: d for d in DETECTORS}


def signature_patterns(steps: list[StepRecord], task: Any | None) -> list[str]:
    """Names of every detector that fires on a trajectory (policy input)."""
    return [d.name for d in DETECTORS if d.fn(steps, task) is not None]


# ─────────────────────────────────────────────────────────────────────
# Heuristic reflector
# ─────────────────────────────────────────────────────────────────────


def classify_run(view: TrajectoryView) -> Classification | None:
    """Per-trajectory reasons a failure is *not* a harness lesson."""
    if view.trajectory.result == "infra_error":
        return "environmental"
    audit = view.evaluation.audit or {}
    if (audit.get("regressions") or 0) > 0 and not view.evaluation.files_out_of_scope:
        # unrelated tests failing while nothing outside scope changed
        return "repository_issue"
    return None


class HeuristicReflector:
    name = "heuristic"

    def __init__(self, *, min_occurrences: int = MIN_OCCURRENCES) -> None:
        self.min_occurrences = min_occurrences

    def reflect(self, views: list[TrajectoryView]) -> ReflectionReport:
        patterns: list[FailurePattern] = []
        discarded: list[str] = []
        n = len(views)
        successes = sum(1 for v in views if v.evaluation.success and not v.evaluation.voided)

        for detector in DETECTORS:
            hits: list[tuple[TrajectoryView, int]] = []
            environmental = 0
            for view in views:
                failed = not (view.evaluation.success and not view.evaluation.voided)
                if detector.failures_only and not failed:
                    continue
                if classify_run(view) is not None:
                    environmental += 1
                    continue
                step = detector.fn(view.steps, view.task)
                if step is not None:
                    hits.append((view, step))
            if not hits:
                continue
            tasks = sorted({v.trajectory.task_id for v, _ in hits})
            classification: Classification = "harness_deficiency"
            if len(hits) < self.min_occurrences:
                classification = "model_randomness"
            elif detector.failures_only and len(tasks) == 1:
                # recurring failure confined to one task: likely the task
                classification = "task_ambiguity"
            pattern = FailurePattern(
                name=detector.name,
                description=detector.description,
                trajectory_ids=[v.trajectory.id for v, _ in hits],
                divergence_steps=[s for _, s in hits],
                task_ids=tasks,
                affected_component=detector.component,  # type: ignore[arg-type]
                scope=detector.scope,  # type: ignore[arg-type]
                classification=classification,
                evidence=[
                    f"{v.trajectory.task_id} step {s}: "
                    f"{_step_summary(next((x for x in v.steps if x.step == s), None))}"
                    for v, s in hits[:5]
                ],
            )
            if classification != "harness_deficiency":
                discarded.append(f"{detector.name}: {classification} ({len(hits)} hits)")
            patterns.append(pattern)
            if environmental:
                discarded.append(f"{detector.name}: {environmental} environmental runs skipped")

        # Most frequent lessons first.
        patterns.sort(key=lambda p: (-p.occurrence_count, p.name))
        strengths = []
        if n:
            strengths.append(f"{successes}/{n} runs verified successful")
        for detector in DETECTORS:
            if not any(p.name == detector.name for p in patterns):
                strengths.append(f"no {detector.name.replace('_', ' ')} observed")
        report = ReflectionReport(
            strengths=strengths,
            failure_patterns=patterns,
            reflector=self.name,
            discarded=discarded,
        )
        report.should_evolve = bool(report.lessons())
        return report


def _step_summary(step: StepRecord | None) -> str:
    if step is None:
        return "?"
    arg = step.input.get("path") or step.input.get("command") or step.input.get("pattern") or ""
    return f"{step.tool}({str(arg)[:60]}){' ✗' if step.is_error else ''}"


# ─────────────────────────────────────────────────────────────────────
# LLM reflector
# ─────────────────────────────────────────────────────────────────────

REFLECTION_PROMPT = """\
You are reviewing coding-agent trajectories to find weaknesses in the
agent's HARNESS (its memory, verification policy, skills, instructions) —
not in the model and not in the tasks.

Classify every failure before proposing anything:
  environmental failure   → not a lesson
  model randomness        → not a lesson
  task ambiguity          → not a lesson
  repository issue        → not a lesson
  harness deficiency      → a lesson

For each recurring pattern (seen in at least {min_occurrences} trajectories), give:
  name
  description
  supporting trajectory ids
  for each: the event sequence number (step) where the pattern first manifests
  affected component: memory | verification | skill | instruction
  scope: local (one decision point) | global (behavior from step 0)
  classification

Respond with ONLY a JSON object:
{{"strengths": [str], "should_evolve": bool,
  "failure_patterns": [{{"name": str, "description": str,
     "trajectory_ids": [str], "divergence_steps": [int],
     "affected_component": str, "scope": str, "classification": str}}]}}

Trajectories (compressed; one line per step: step|tool|argument|✗ on error):
{trajectories}
"""


def compress_trajectory(view: TrajectoryView, *, max_steps: int = 60) -> str:
    ev = view.evaluation
    header = (
        f"## trajectory {view.trajectory.id} task={view.trajectory.task_id} "
        f"result={view.trajectory.result} success={ev.success} voided={ev.voided} "
        f"tool_calls={ev.tool_calls} redundant_reads={ev.redundant_reads}"
    )
    lines = [header]
    for step in view.steps[:max_steps]:
        if step.kind != "tool_call":
            continue
        lines.append(f"{step.step}|{_step_summary(step)}")
    if len(view.steps) > max_steps:
        lines.append(f"… {len(view.steps) - max_steps} more steps")
    return "\n".join(lines)


class LLMReflector:
    name = "llm"

    def __init__(self, runner: Callable[[str], str], *, min_occurrences: int = MIN_OCCURRENCES):
        self.runner = runner
        self.min_occurrences = min_occurrences

    def reflect(self, views: list[TrajectoryView]) -> ReflectionReport:
        prompt = REFLECTION_PROMPT.format(
            min_occurrences=self.min_occurrences,
            trajectories="\n\n".join(compress_trajectory(v) for v in views),
        )
        raw = self.runner(prompt)
        data = _extract_json(raw)
        steps_by_traj = {v.trajectory.id: {s.step for s in v.steps} for v in views}
        task_by_traj = {v.trajectory.id: v.trajectory.task_id for v in views}
        patterns: list[FailurePattern] = []
        discarded: list[str] = []
        for item in data.get("failure_patterns", []):
            try:
                pattern = FailurePattern.model_validate(item)
            except ValidationError as exc:
                discarded.append(f"invalid pattern: {exc.errors()[0]['msg']}")
                continue
            kept_t, kept_s = [], []
            for traj, step in zip(pattern.trajectory_ids, pattern.divergence_steps):
                if traj in steps_by_traj and step in steps_by_traj[traj]:
                    kept_t.append(traj)
                    kept_s.append(step)
            if len(kept_t) != len(pattern.trajectory_ids):
                discarded.append(
                    f"{pattern.name}: {len(pattern.trajectory_ids) - len(kept_t)} "
                    "citations with nonexistent trajectory/step dropped"
                )
            if len(kept_t) < self.min_occurrences:
                discarded.append(f"{pattern.name}: below {self.min_occurrences} valid occurrences")
                continue
            pattern.trajectory_ids, pattern.divergence_steps = kept_t, kept_s
            pattern.task_ids = sorted({task_by_traj[t] for t in kept_t})
            patterns.append(pattern)
        report = ReflectionReport(
            strengths=list(data.get("strengths", [])),
            failure_patterns=patterns,
            reflector=self.name,
            discarded=discarded,
        )
        report.should_evolve = bool(report.lessons())
        return report


def _extract_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and "result" in value and isinstance(value["result"], str):
            return _extract_json(value["result"])  # claude --output-format json envelope
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
