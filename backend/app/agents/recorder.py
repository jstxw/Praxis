"""Drive an adapter and capture its trajectory, one checkpoint per step.

This is worker-side code: it runs agent code and produces a *staged*
trajectory — steps, per-step workspace snapshots and adapter resume
state — but never a score. Scoring belongs to the trusted plane
(``app.refinement.evaluator``), which reads the staged trajectory, runs
the hidden verifier and writes ``trajectories`` + ``task_evaluations``.

Step 0 is the injected-context step: the checkpoint "before the agent
did anything". Forking at step 0 is equivalent to a from-scratch run
with a shared starting workspace; forks at ``d−1 ≥ 1`` share a real
prefix.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.agents.base import (
    CodingAgentAdapter,
    HarnessBinding,
    ResumePoint,
    TaskSpec,
)
from app.continual.experience import StepRecord
from app.continual.snapshots import SnapshotStore


@dataclass
class StagedTrajectory:
    """Everything a worker hands to the trusted plane for one run."""

    trajectory_id: str
    task_id: str
    harness_id: str
    memory_version: str
    agent: str
    seed: int
    status: str  # completed | timeout | infra_error
    base_snapshot: str
    final_snapshot: str
    steps: list[StepRecord]
    tool_calls: int  # including an inherited fork prefix
    tokens: int  # including an inherited fork prefix
    context_tokens: int
    model_version: str = "unknown"
    fork_parent: str | None = None
    fork_step: int | None = None
    error: str | None = None
    wall_time_s: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StagedTrajectory":
        payload = dict(data)
        payload["steps"] = [StepRecord(**s) for s in payload["steps"]]
        return cls(**payload)


def populate_workspace(task: TaskSpec, workspace: Path) -> None:
    """Fresh copy of the task's pristine workspace (nothing else)."""
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(
        task.workspace_dir,
        workspace,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )


async def record_run(
    adapter: CodingAgentAdapter,
    task: TaskSpec,
    harness: HarnessBinding,
    workspace: Path,
    snapshots: SnapshotStore,
    *,
    seed: int,
    resume: ResumePoint | None = None,
    trajectory_id: str | None = None,
    timeout_s: float = 900.0,
) -> StagedTrajectory:
    """Run one task (or one fork tail) and stage its trajectory."""
    started = time.monotonic()
    workspace = Path(workspace)
    base_snapshot = snapshots.snapshot(task.workspace_dir)
    steps: list[StepRecord] = []

    if resume is None:
        populate_workspace(task, workspace)
        step = 0
        tool_calls = 0
        tokens = harness.context.tokens
        steps.append(
            StepRecord(
                step=0,
                kind="context",
                input={"context": harness.context.to_json()},
                output=harness.context.text,
                tokens=harness.context.tokens,
                snapshot_id=snapshots.snapshot(workspace),
                agent_state={},
            )
        )
    else:
        snapshots.restore(resume.snapshot_id, workspace)
        step = resume.step
        tool_calls = resume.prefix_tool_calls
        tokens = resume.prefix_tokens

    status, error = "completed", None
    model_version = "unknown"
    saw_usage = False
    limit = harness.snapshot.control.max_tool_calls

    await adapter.start_task(task, workspace, harness, resume=resume, seed=seed)

    async def consume() -> None:
        nonlocal step, tool_calls, tokens, status, error, model_version, saw_usage
        async for event in adapter.stream_events():
            if event.model_version:
                model_version = event.model_version
            if event.kind == "error":
                status, error = "infra_error", event.output
                return
            if event.kind == "budget_exhausted":
                # A spend/turn cap is a timeout, not an infrastructure failure.
                status, error = "timeout", f"agent budget exhausted ({event.output})"
                return
            if event.kind == "usage":
                saw_usage = True
                tokens += event.tokens
                continue
            if event.kind != "tool_call":
                continue
            step += 1
            tool_calls += 1
            tokens += event.tokens
            steps.append(
                StepRecord(
                    step=step,
                    kind="tool_call",
                    tool=event.tool,
                    input=event.input,
                    output=event.output[:4000],
                    is_error=event.is_error,
                    tokens=event.tokens,
                    snapshot_id=snapshots.snapshot(workspace),
                    agent_state=event.agent_state,
                )
            )
            if tool_calls >= limit:
                status = "timeout"
                error = f"tool-call budget exhausted ({limit})"
                await adapter.terminate()
                return

    try:
        await asyncio.wait_for(consume(), timeout=timeout_s)
    except asyncio.TimeoutError:
        status, error = "timeout", f"wall-clock budget exhausted ({timeout_s}s)"
        await adapter.terminate()
    except Exception as exc:  # noqa: BLE001 — agent failure is data
        status, error = "infra_error", f"{type(exc).__name__}: {exc}"
        await adapter.terminate()

    return StagedTrajectory(
        trajectory_id=trajectory_id or uuid.uuid4().hex,
        task_id=task.id,
        harness_id=harness.harness_id,
        memory_version=harness.memory_version,
        agent=adapter.name,
        seed=seed,
        status=status,
        base_snapshot=base_snapshot,
        final_snapshot=snapshots.snapshot(workspace),
        steps=steps,
        tool_calls=tool_calls,
        tokens=tokens,
        context_tokens=harness.context.tokens,
        model_version=model_version,
        fork_parent=resume.trajectory_id if resume else None,
        fork_step=resume.step if resume else None,
        error=error,
        wall_time_s=round(time.monotonic() - started, 3),
        # Adapters that report usage only at the end (Claude Code's final
        # `result` event) have no token count for a run cut off early.
        metadata={
            "tokens_complete": saw_usage or not getattr(adapter, "reports_usage_at_end", False)
        },
    )
