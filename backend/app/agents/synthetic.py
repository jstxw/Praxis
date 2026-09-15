"""Seeded synthetic coding agent — a pipeline instrument, NOT a result.

This agent exists so the refinement pipeline (trajectory capture,
forking, evaluation, statistics, gate, loop) can be exercised end to end
offline, deterministically, and at a scale no live model budget allows.
It is an *oracle simulator*: when it "fixes" a task it copies the
corpus's gold files. Every trajectory it produces is tagged
``agent="synthetic"``, and no number it produces may be presented as a
measurement of harness refinement on a real agent (VISION §6 rule 2).

What it models, and why that is enough for pipeline validation:

- **Real workspace, real verification.** Reads, edits and test runs
  touch the real task workspace; its test runs execute pytest for real.
  The trusted-plane verifier later re-runs the pristine tests itself.
- **Stochastic behavior with habits** a harness can plausibly change:
  exploring unrelated files, re-reading files, running the full suite
  instead of the task's tests, redundant confirmation runs, blind
  retries after a failure, submitting untested, occasionally tampering
  with tests.
- **Harness sensitivity through the injected context only.** The agent
  never sees harness ids; it recognizes directives in the
  ``<HARNESS_CONTEXT>`` text and complies with a per-component
  probability (verification > memory > skill > instruction — an
  *assumption* echoing the published ablation, stated here, not a
  finding). A directive dropped by the context budget has no effect.
- **Resumable at any step.** Decision state is explicit and serialized
  into every event's ``agent_state``, so a fork restores it exactly and
  continues under a different harness with a fresh tail seed. That is
  what makes E2 (fork vs scratch variance) runnable on it.
"""

from __future__ import annotations

import asyncio
import random
import re
import shlex
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from app.agents.base import (
    TOOL_BASH,
    TOOL_COMPLETE,
    TOOL_GREP,
    TOOL_PATCH,
    TOOL_READ,
    AgentEvent,
    HarnessBinding,
    ResumePoint,
    TaskSpec,
)
from app.continual.harness_object import estimate_tokens

AGENT_NAME = "synthetic"
MODEL_VERSION = "synthetic-oracle-v1"


@dataclass
class SyntheticProfile:
    """Behavioral habits of the simulated agent (baseline, no harness)."""

    p_correct: dict[str, float] = field(
        default_factory=lambda: {"easy": 0.75, "medium": 0.55, "hard": 0.35}
    )
    inspect_bonus: float = 0.25
    p_explore: float = 0.7
    p_reread: float = 0.35
    p_read_tests: float = 0.8
    p_run_tests: float = 0.7
    p_full_suite: float = 0.65
    p_confirm_rerun: float = 0.4
    p_blind_retry: float = 0.7
    p_tamper: float = 0.03
    compliance: dict[str, float] = field(
        default_factory=lambda: {
            "verification": 0.9,
            "memory": 0.85,
            "skill": 0.75,
            "instruction": 0.5,
        }
    )
    step_overhead_tokens: int = 250


# ── directive recognition ────────────────────────────────────────────

_RULE_EFFECTS = {
    "targeted_tests": "targeted",
    "run_tests": "run_tests",
    "inspect_failure": "inspect",
    "no_test_edits": "no_tamper",
}
_KEYWORD_EFFECTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"without running tests|skip (the )?tests|do not run tests"), "skip_tests"),
    (re.compile(r"do not read|don't read|avoid reading"), "no_reads"),
    (re.compile(r"by any means|edit the tests|modify the tests"), "tamper"),
    (re.compile(r"targeted|only the (task'?s )?test|not the full suite"), "targeted"),
    (re.compile(r"read the fail|inspect the fail|before (editing|retrying) again|no blind retr"), "inspect"),
    (re.compile(r"read (each|a) file once|re-?read|already read"), "no_reread"),
    (re.compile(r"run (the )?tests before submit|verify before submit"), "run_tests"),
    (re.compile(r"never (modify|edit) (existing )?tests"), "no_tamper"),
]


def recognize_directives(context_text: str, task: TaskSpec) -> dict[str, str]:
    """Map effect → strongest source component found in the context."""
    rank = {"verification": 4, "memory": 3, "skill": 2, "instruction": 1}
    found: dict[str, str] = {}

    def note(effect: str, component: str) -> None:
        if effect not in found or rank[component] > rank[found[effect]]:
            found[effect] = component

    section = None
    for raw in context_text.splitlines():
        line = raw.strip()
        header = {
            "Repository facts:": "memory",
            "Active skills:": "skill",
            "Verification:": "verification",
            "Instructions:": "instruction",
        }.get(line)
        if header:
            section = header
            continue
        if not line.startswith("- ") or section is None:
            continue
        body = line[2:].lower()
        if section == "verification":
            match = re.match(r"\[(\w+)\]", body)
            if match and match.group(1) in _RULE_EFFECTS:
                note(_RULE_EFFECTS[match.group(1)], "verification")
            continue
        if section == "memory":
            scope_names = {Path(p).name.lower() for p in task.scope_files}
            test_names = {Path(p).name.lower() for p in task.test_files}
            if any(name in body for name in scope_names):
                note("know_location", "memory")
            if any(name in body for name in test_names):
                note("targeted", "memory")
        for pattern, effect in _KEYWORD_EFFECTS:
            if pattern.search(body):
                note(effect, section)
    return found


# ── the agent ────────────────────────────────────────────────────────


def _initial_state() -> dict[str, Any]:
    return {
        "phase": "orient",
        "queue": [],
        "attempts": 0,
        "fixed_correct": False,
        "inspected_since_fail": False,
        "last_test_passed": None,
        "tests_run": 0,
        "confirmed": False,
        "tokens": 0,
        "tool_calls": 0,
    }


class SyntheticAgent:
    """Implements :class:`CodingAgentAdapter` over a real workspace."""

    name = AGENT_NAME
    supports_fork = True

    def __init__(self, profile: SyntheticProfile | None = None) -> None:
        self.profile = profile or SyntheticProfile()
        self._terminated = False
        self._task: TaskSpec | None = None
        self._workspace: Path | None = None
        self._rng = random.Random(0)
        self._state: dict[str, Any] = _initial_state()
        self._directives: dict[str, str] = {}
        self._context_tokens = 0

    async def start_task(
        self,
        task: TaskSpec,
        workspace: Path,
        harness: HarnessBinding,
        *,
        resume: ResumePoint | None = None,
        seed: int = 0,
    ) -> str:
        self._task = task
        self._workspace = Path(workspace)
        self._rng = random.Random(seed)
        self._state = (
            _deepcopy_state(resume.agent_state) if resume is not None else _initial_state()
        )
        self._max_retries = harness.snapshot.control.max_verify_retries
        await self.inject_context(harness.context.text)
        self._context_tokens = harness.context.tokens
        self._terminated = False
        return f"synthetic-{uuid.uuid4().hex[:12]}"

    async def inject_context(self, context: str) -> None:
        assert self._task is not None
        self._directives = recognize_directives(context, self._task)

    async def terminate(self) -> None:
        self._terminated = True

    # ── compliance ───────────────────────────────────────────────────

    def _obeys(self, effect: str) -> bool:
        component = self._directives.get(effect)
        if component is None:
            return False
        return self._rng.random() < self.profile.compliance[component]

    # ── planning ─────────────────────────────────────────────────────

    def _workspace_files(self) -> list[str]:
        assert self._workspace is not None
        files = []
        for path in sorted(self._workspace.rglob("*.py")):
            rel = path.relative_to(self._workspace).as_posix()
            if "__pycache__" in rel or rel.endswith("__init__.py"):
                continue
            files.append(rel)
        return files

    def _existing(self, rels: tuple[str, ...] | list[str]) -> list[str]:
        assert self._workspace is not None
        return [r for r in rels if (self._workspace / r).is_file()]

    def _plan(self) -> None:
        """Fill the action queue for the current phase."""
        s, p, task, rng = self._state, self.profile, self._task, self._rng
        assert task is not None
        phase = s["phase"]
        queue: list[list[Any]] = []
        no_reads = self._obeys("no_reads")

        if phase == "orient":
            if not self._obeys("know_location") and not no_reads and rng.random() < p.p_explore:
                keyword = Path(task.scope_files[0]).stem if task.scope_files else "def"
                queue.append(["grep", keyword])
                others = [
                    f for f in self._workspace_files()
                    if f not in task.scope_files and f not in task.test_files
                ]
                rng.shuffle(others)
                for rel in others[: rng.randint(1, 3)]:
                    queue.append(["read", rel])
            s["phase"] = "read_scope"
        elif phase == "read_scope":
            if not no_reads:
                for rel in self._existing(task.scope_files):
                    queue.append(["read", rel])
            s["phase"] = "read_tests"
        elif phase == "read_tests":
            if not no_reads and rng.random() < p.p_read_tests:
                for rel in self._existing(task.test_files):
                    queue.append(["read", rel])
            s["phase"] = "fix"
        elif phase == "fix":
            s["attempts"] += 1
            if (
                not no_reads
                and not self._obeys("no_reread")
                and rng.random() < p.p_reread
                and self._existing(task.scope_files)
            ):
                queue.append(["read", rng.choice(self._existing(task.scope_files))])
            tamper_p = p.p_tamper * (8.0 if self._obeys("tamper") else 1.0)
            if not self._obeys("no_tamper") and rng.random() < tamper_p and task.test_files:
                queue.append(["tamper", task.test_files[0]])
            p_fix = p.p_correct.get(task.difficulty, 0.5)
            if s["inspected_since_fail"]:
                p_fix = min(0.97, p_fix + p.inspect_bonus)
            if rng.random() < p_fix:
                queue.append(["write_gold"])
            else:
                target = task.scope_files[0] if task.scope_files else "solution.py"
                queue.append(["write_sloppy", target])
            s["inspected_since_fail"] = False
            s["phase"] = "verify"
        elif phase == "verify":
            skip = self._obeys("skip_tests")
            run = not skip and (self._obeys("run_tests") or rng.random() < p.p_run_tests)
            if not run:
                s["phase"] = "submit"
                return self._plan()
            mode = "targeted" if self._obeys("targeted") else (
                "full" if rng.random() < p.p_full_suite else "targeted"
            )
            queue.append(["test", mode, "verify"])
            s["phase"] = "after_verify"
        elif phase == "confirm":
            if (
                not s["confirmed"]
                and not self._obeys("targeted")
                and rng.random() < p.p_confirm_rerun
            ):
                queue.append(["test", "full", "confirm"])
            s["confirmed"] = True
            s["phase"] = "submit"
        elif phase == "retry":
            inspect = self._obeys("inspect") or rng.random() > p.p_blind_retry
            if inspect and not no_reads:
                for rel in self._existing(task.scope_files):
                    queue.append(["read", rel])
                s["inspected_since_fail"] = True
            s["phase"] = "fix"
        elif phase == "submit":
            queue.append(["submit"])
            s["phase"] = "done"
        s["queue"] = queue

    # ── execution ────────────────────────────────────────────────────

    async def stream_events(self) -> AsyncIterator[AgentEvent]:
        s = self._state
        yield AgentEvent(kind="init", model_version=MODEL_VERSION)
        guard = 0
        while s["phase"] != "done" or s["queue"]:
            if self._terminated:
                return
            guard += 1
            if guard > 500:  # a planning bug must not hang an evaluation
                yield AgentEvent(kind="error", output="synthetic agent step guard hit",
                                 is_error=True)
                return
            if not s["queue"]:
                if s["phase"] == "after_verify":
                    self._route_after_verify()
                    continue
                self._plan()
                continue
            action = s["queue"].pop(0)
            event = await asyncio.to_thread(self._execute, action)
            s["tool_calls"] += 1
            s["tokens"] += event.tokens
            event.agent_state = _deepcopy_state(s)
            yield event

    def _route_after_verify(self) -> None:
        s = self._state
        if s["last_test_passed"]:
            s["phase"] = "confirm"
        elif s["attempts"] <= self._max_retries:
            s["phase"] = "retry"
        else:
            s["phase"] = "submit"

    def _execute(self, action: list[Any]) -> AgentEvent:
        task, ws, s, p = self._task, self._workspace, self._state, self.profile
        assert task is not None and ws is not None
        kind = action[0]
        overhead = p.step_overhead_tokens + self._context_tokens // 4

        if kind == "grep":
            pattern = str(action[1])
            hits = []
            for rel in self._workspace_files():
                text = (ws / rel).read_text(errors="replace")
                hits.extend(
                    f"{rel}:{i}" for i, line in enumerate(text.splitlines(), 1)
                    if pattern in line
                )
            out = "\n".join(hits[:50])
            return AgentEvent(kind="tool_call", tool=TOOL_GREP, input={"pattern": pattern},
                              output=out, tokens=overhead + estimate_tokens(out))
        if kind == "read":
            rel = str(action[1])
            path = ws / rel
            text = path.read_text(errors="replace") if path.is_file() else ""
            return AgentEvent(kind="tool_call", tool=TOOL_READ, input={"path": rel},
                              output=text[:4000], is_error=not path.is_file(),
                              tokens=overhead + estimate_tokens(text))
        if kind == "write_gold":
            written = []
            for src in sorted(task.gold_dir.rglob("*")):
                if src.is_file():
                    rel = src.relative_to(task.gold_dir).as_posix()
                    dest = ws / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(src.read_bytes())
                    written.append(rel)
            s["fixed_correct"] = True
            size = sum(estimate_tokens((ws / r).read_text(errors="replace")) for r in written)
            return AgentEvent(kind="tool_call", tool=TOOL_PATCH,
                              input={"path": ",".join(written), "attempt": s["attempts"]},
                              output="patched", tokens=overhead + size)
        if kind == "write_sloppy":
            rel = str(action[1])
            dest = ws / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("a") as fh:
                fh.write(f"\n# attempted fix {s['attempts']}\n")
            return AgentEvent(kind="tool_call", tool=TOOL_PATCH,
                              input={"path": rel, "attempt": s["attempts"]},
                              output="patched", tokens=overhead + 60)
        if kind == "tamper":
            rel = str(action[1])
            dest = ws / rel
            if dest.is_file():
                original = dest.read_text()
                dest.write_text(
                    "import pytest\npytestmark = pytest.mark.skip(reason='flaky')\n" + original
                )
            return AgentEvent(kind="tool_call", tool=TOOL_PATCH, input={"path": rel},
                              output="patched", tokens=overhead + 40)
        if kind == "test":
            mode = str(action[1])
            command = task.full_test_command if mode == "full" else task.test_command
            passed, output = _run_pytest(ws, command)
            s["tests_run"] += 1
            if action[2] == "verify":
                s["last_test_passed"] = passed
            return AgentEvent(kind="tool_call", tool=TOOL_BASH,
                              input={"command": command, "mode": mode},
                              output=output[-2000:], is_error=not passed,
                              tokens=overhead + estimate_tokens(output[-2000:]))
        if kind == "submit":
            return AgentEvent(kind="tool_call", tool=TOOL_COMPLETE, input={},
                              output="complete", tokens=overhead)
        raise ValueError(f"unknown synthetic action {action!r}")


def _run_pytest(workspace: Path, command: str, timeout_s: int = 120) -> tuple[bool, str]:
    """Run a ``pytest …`` command with this interpreter's pytest."""
    argv = shlex.split(command)
    if argv and argv[0] == "pytest":
        argv = [sys.executable, "-m", "pytest", *argv[1:]]
    try:
        proc = subprocess.run(
            argv + ["-p", "no:cacheprovider"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, "[timeout]"
    return proc.returncode == 0, proc.stdout + proc.stderr


def _deepcopy_state(state: dict[str, Any]) -> dict[str, Any]:
    return {k: ([list(a) for a in v] if k == "queue" else v) for k, v in state.items()}
