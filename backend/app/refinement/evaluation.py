"""Candidate evaluation on the durable runtime (ARCHITECTURE §4, §6).

Every agent execution in a comparison is a **branch** in ``branch_runs``:

    fork_from_checkpoint(trajectory=T, step=d−1, mods=H1)   → branch
    fork_from_checkpoint(trajectory=T, step=d−1, mods=H2)   → branch
    …and the parent arm H0 runs the same tail from the same checkpoint.

Workers claim branches with a lease and a fencing token (I5), run the
agent, stage the trajectory, and record it through the store's fenced,
exactly-once ``record_iteration`` (I1). A worker killed mid-run leaves
an expired lease; another worker reclaims with a new fence and reruns
the branch from the same checkpoint. The stale worker's writes are
rejected by the data layer.

Then the **trusted plane** (``collect``) reads the authoritative
iteration log, runs the hidden verifier, and writes trajectories and
evaluations. Workers never write scores; the trusted plane never runs
agent code.

Comparison design (DESIGN §6):

- **paired** — same task, same base, same pinned memory version, both arms;
- **repeated** — ``reps`` per (task, arm);
- **interleaved** — the schedule is rep → shuffled tasks → shuffled arms,
  and claims are FIFO, so no arm runs "all first";
- **independent seeds per arm** — a real model gives no seed control, so
  sharing seeds across arms would understate from-scratch variance and
  flatter the fork method. Forking shares the *prefix*, never the tail.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import shutil
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.agents.base import CodingAgentAdapter, HarnessBinding, ResumePoint, TaskSpec
from app.agents.recorder import StagedTrajectory, record_run
from app.continual.experience import (
    EvaluationRecord,
    ExperienceStore,
    TrajectoryRecord,
)
from app.continual.harness_object import render_context
from app.continual.snapshots import SnapshotStore
from app.meta_harness.store import StaleFenceError, StateStore
from app.refinement.evaluator import Verifier, evaluate_staged

EVAL_RUN_PREFIX = "hx-"
LOWER_IS_BETTER = {
    "tool_calls": True,
    "tokens": True,
    "redundant_reads": True,
    "diff_size": True,
    "files_out_of_scope": True,
    "regressions": True,
    "context_tokens": True,
    "success": False,
}


class MemoryNotFrozen(RuntimeError):
    """An arm's memory is not the pinned version (or a mutation of it)."""


class ForkUnsupported(RuntimeError):
    """The adapter cannot resume from a checkpoint."""


@dataclass(frozen=True)
class Arm:
    name: str
    harness_id: str
    memory_version: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunSpec:
    index: int
    arm: str
    task_id: str
    rep: int
    seed: int
    method: str  # scratch | fork
    fork_trajectory: str | None = None
    fork_step: int | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RunSpec":
        return cls(**data)


@dataclass
class EvaluationPlan:
    run_id: str
    arms: list[Arm]
    specs: list[RunSpec]
    pinned_memory: str
    method: str
    task_ids: list[str]
    reps: int

    def arm(self, name: str) -> Arm:
        return next(a for a in self.arms if a.name == name)


@dataclass
class RunResult:
    spec: RunSpec
    trajectory: TrajectoryRecord
    evaluation: EvaluationRecord


def nearest_resumable_step(steps: list[Any], step: int) -> int | None:
    """Latest checkpoint at or before ``step`` an adapter can resume from.

    Not every step is a fork point: a step inside a batch of parallel tool
    calls has no resume state (resuming there would hand the model half a
    batch of results).
    """
    candidates = [
        s.step for s in steps
        if s.step <= step and s.snapshot_id and s.agent_state is not None
    ]
    return max(candidates) if candidates else None


def derive_seed(*parts: Any) -> int:
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:12], 16)


def new_run_id(label: str) -> str:
    return f"{EVAL_RUN_PREFIX}{label}-{os.urandom(4).hex()}"


def plan_comparison(
    *,
    run_id: str,
    arms: list[Arm],
    task_ids: list[str],
    reps: int,
    method: str,
    pinned_memory: str,
    seed: int,
    fork_points: dict[str, tuple[str, int]] | None = None,
) -> EvaluationPlan:
    """Interleaved paired schedule; fork plans need a checkpoint per task."""
    if not run_id.startswith(EVAL_RUN_PREFIX):
        raise ValueError(f"evaluation run ids must start with {EVAL_RUN_PREFIX!r}")
    if method not in {"scratch", "fork"}:
        raise ValueError(f"unknown method {method!r}")
    if method == "fork":
        missing = [t for t in task_ids if not fork_points or t not in fork_points]
        if missing:
            raise ValueError(f"fork plan has no checkpoint for tasks {missing}")
    rng = random.Random(seed)
    specs: list[RunSpec] = []
    for rep in range(reps):
        tasks = list(task_ids)
        rng.shuffle(tasks)
        for task_id in tasks:
            order = list(arms)
            rng.shuffle(order)
            for arm in order:
                fork = (fork_points or {}).get(task_id) if method == "fork" else None
                specs.append(
                    RunSpec(
                        index=len(specs),
                        arm=arm.name,
                        task_id=task_id,
                        rep=rep,
                        seed=derive_seed(seed, arm.name, task_id, rep),
                        method=method,
                        fork_trajectory=fork[0] if fork else None,
                        fork_step=fork[1] if fork else None,
                    )
                )
    return EvaluationPlan(
        run_id=run_id,
        arms=list(arms),
        specs=specs,
        pinned_memory=pinned_memory,
        method=method,
        task_ids=list(task_ids),
        reps=reps,
    )


async def assert_memory_frozen(xp: ExperienceStore, plan: EvaluationPlan) -> None:
    """Every arm runs on the pinned memory, or on a direct mutation of it
    (a memory-mutation candidate). Nothing accumulated mid-comparison."""
    for arm in plan.arms:
        if arm.memory_version == plan.pinned_memory:
            continue
        version = await xp.get_memory_version(arm.memory_version)
        if version is None or version.parent_id != plan.pinned_memory:
            raise MemoryNotFrozen(
                f"arm {arm.name} uses memory {arm.memory_version}, which is neither "
                f"the pinned version {plan.pinned_memory} nor a mutation of it"
            )


class EvaluationRuntime:
    def __init__(
        self,
        *,
        state: StateStore,
        xp: ExperienceStore,
        snapshots: SnapshotStore,
        tasks: dict[str, TaskSpec],
        adapter_factory: Callable[[], CodingAgentAdapter],
        verifier: Verifier,
        work_root: Path,
        lease_ttl_s: float = 60.0,
        poll_interval_s: float = 0.2,
        run_timeout_s: float = 900.0,
        verify_concurrency: int = 4,
    ) -> None:
        self.verify_concurrency = verify_concurrency
        self.state = state
        self.xp = xp
        self.snapshots = snapshots
        self.tasks = tasks
        self.adapter_factory = adapter_factory
        self.verifier = verifier
        self.work_root = Path(work_root)
        self.lease_ttl_s = lease_ttl_s
        self.poll_interval_s = poll_interval_s
        self.run_timeout_s = run_timeout_s

    # ── submit ───────────────────────────────────────────────────────

    async def submit(self, plan: EvaluationPlan) -> int:
        """Create one branch per spec. Idempotent: resubmitting is a no-op."""
        await assert_memory_frozen(self.xp, plan)
        if plan.method == "fork" and not self.adapter_factory().supports_fork:
            raise ForkUnsupported("adapter cannot resume from checkpoints; use scratch")
        created = 0
        arms = {a.name: a for a in plan.arms}
        for spec in plan.specs:
            branch_id = f"{plan.run_id}:{spec.index}"
            if await self.state.get_branch(branch_id) is not None:
                continue
            await self.state.create_branch(
                branch_id=branch_id,
                run_id=plan.run_id,
                thread_id=branch_id,
                parent_thread_id=spec.fork_trajectory or f"task:{spec.task_id}",
                parent_checkpoint_id=(
                    f"{spec.fork_trajectory}@{spec.fork_step}" if spec.fork_trajectory else None
                ),
                mods={"spec": spec.to_json(), "arm": arms[spec.arm].to_json()},
                name=f"{spec.arm} {spec.task_id} rep{spec.rep}",
            )
            created += 1
        return created

    # ── workers (untrusted side: run agents, stage, fenced record) ──

    async def work(self, run_id: str, *, n_workers: int = 1) -> int:
        """Run in-process workers until every branch of ``run_id`` is terminal."""
        results = await asyncio.gather(
            *[self._worker_loop(run_id, f"{socket.gethostname()}-{os.getpid()}-w{i}")
              for i in range(n_workers)]
        )
        return sum(results)

    async def _worker_loop(self, run_id: str, worker_id: str) -> int:
        processed = 0
        while True:
            row = await self.state.claim_next_branch(
                worker_id=worker_id, lease_ttl_s=self.lease_ttl_s, run_prefix=run_id
            )
            if row is None:
                branches = await self.state.list_branches(run_id=run_id)
                if all(b.status in {"completed", "failed", "cancelled"} for b in branches):
                    return processed
                await asyncio.sleep(self.poll_interval_s)
                continue
            await self.execute_branch(row)
            processed += 1

    async def execute_branch(self, row: Any) -> None:
        """One claimed branch under heartbeat + fence (mirrors app/worker.py)."""
        fence = row.lease_generation

        async def heartbeat_forever() -> None:
            interval = max(self.lease_ttl_s / 3.0, 0.05)
            while True:
                await asyncio.sleep(interval)
                await self.state.heartbeat(
                    branch_id=row.branch_id, fence=fence, lease_ttl_s=self.lease_ttl_s
                )

        exec_task = asyncio.create_task(self._run_branch(row, fence))
        hb_task = asyncio.create_task(heartbeat_forever())
        try:
            done, _ = await asyncio.wait({exec_task, hb_task}, return_when=asyncio.FIRST_COMPLETED)
            if hb_task in done:
                exec_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await exec_task
                with contextlib.suppress(StaleFenceError):
                    hb_task.result()
                return
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
            try:
                status = exec_task.result()
            except StaleFenceError:
                return
            except Exception as exc:  # noqa: BLE001 — infra failure is data
                with contextlib.suppress(StaleFenceError):
                    await self.state.finish_branch(
                        branch_id=row.branch_id, fence=fence, status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                return
            with contextlib.suppress(StaleFenceError):
                await self.state.finish_branch(
                    branch_id=row.branch_id, fence=fence, status="completed",
                    result={"staged_status": status},
                )
        finally:
            for task in (exec_task, hb_task):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    async def _run_branch(self, row: Any, fence: int) -> str:
        spec = RunSpec.from_json(row.mods["spec"])
        arm = Arm(**row.mods["arm"])
        task = self.tasks[spec.task_id]
        adapter = self.adapter_factory()
        binding = await self.bind(arm, task)
        resume = await self.resume_point(spec) if spec.method == "fork" else None
        workspace = self.work_root / f"{row.branch_id.replace(':', '_')}-g{fence}"
        try:
            staged = await record_run(
                adapter, task, binding, workspace, self.snapshots,
                seed=spec.seed, resume=resume,
                trajectory_id=hashlib.sha256(f"{row.branch_id}#g{fence}".encode()).hexdigest()[:32],
                timeout_s=self.run_timeout_s,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        staged.metadata.update({"run_id": row.run_id, "spec_index": spec.index,
                                "arm": arm.name, "rep": spec.rep, "fence": fence})
        blob = self.snapshots.put_bytes(json.dumps(staged.to_json(), default=str).encode())
        # Fenced exactly-once record: a reclaimed worker gets StaleFenceError
        # here and its staged trajectory is never evaluated (I1, I5).
        await self.state.record_iteration(
            run_id=row.run_id,
            iteration=spec.index,
            candidate=arm.name,
            row={"staged_blob": blob, "trajectory_id": staged.trajectory_id,
                 "branch_id": row.branch_id, "fence": fence, "status": staged.status},
            branch_id=row.branch_id,
            fence=fence,
        )
        return staged.status

    async def bind(self, arm: Arm, task: TaskSpec) -> HarnessBinding:
        harness = await self.xp.get_harness(arm.harness_id)
        memory = await self.xp.get_memory_version(arm.memory_version)
        if harness is None or memory is None:
            raise KeyError(f"arm {arm.name}: unknown harness or memory version")
        context = render_context(harness.snapshot, memory.entries, task_text=task.instruction)
        return HarnessBinding(
            harness_id=harness.id,
            memory_version=memory.id,
            snapshot=harness.snapshot,
            memory=memory.entries,
            context=context,
        )

    async def resume_point(self, spec: RunSpec) -> ResumePoint:
        assert spec.fork_trajectory is not None and spec.fork_step is not None
        steps = await self.xp.full_steps(spec.fork_trajectory)
        prefix = [s for s in steps if s.step <= spec.fork_step]
        checkpoint = next((s for s in prefix if s.step == spec.fork_step), None)
        if checkpoint is None or checkpoint.snapshot_id is None or checkpoint.agent_state is None:
            raise ForkUnsupported(
                f"trajectory {spec.fork_trajectory} has no resumable checkpoint at "
                f"step {spec.fork_step}"
            )
        return ResumePoint(
            trajectory_id=spec.fork_trajectory,
            step=spec.fork_step,
            snapshot_id=checkpoint.snapshot_id,
            agent_state=checkpoint.agent_state,
            prefix_tool_calls=sum(1 for s in prefix if s.kind == "tool_call"),
            prefix_tokens=sum(s.tokens for s in prefix),
        )

    # ── trusted plane ────────────────────────────────────────────────

    async def collect(self, plan: EvaluationPlan) -> list[RunResult]:
        """Evaluate every authoritative iteration record exactly once.

        Verification (sandboxed test runs) is concurrent; experience-store
        writes stay sequential so one store connection is never used by
        two transactions at once.
        """
        specs = {s.index: s for s in plan.specs}
        results: list[RunResult] = []
        pending: list[tuple[dict[str, Any], RunSpec, StagedTrajectory, list[Any]]] = []
        for row in await self.state.list_iterations(run_id=plan.run_id):
            spec = specs[int(row["branch_id"].rsplit(":", 1)[1])]
            arm = plan.arm(spec.arm)
            trajectory = await self.xp.get_trajectory(row["trajectory_id"])
            evaluation = await self.xp.get_evaluation(row["trajectory_id"]) if trajectory else None
            if trajectory is not None and evaluation is not None:
                results.append(RunResult(spec=spec, trajectory=trajectory, evaluation=evaluation))
                continue
            staged = StagedTrajectory.from_json(
                json.loads(self.snapshots.get_bytes(row["staged_blob"]))
            )
            if staged.memory_version != arm.memory_version:
                raise MemoryNotFrozen(
                    f"trajectory {staged.trajectory_id} ran with memory "
                    f"{staged.memory_version}, arm pins {arm.memory_version}"
                )
            prefix: list[Any] = []
            if staged.fork_parent is not None:
                prefix = [
                    s for s in await self.xp.full_steps(staged.fork_parent)
                    if s.step <= (staged.fork_step or 0)
                ]
            pending.append((row, spec, staged, prefix))

        gate = asyncio.Semaphore(max(1, self.verify_concurrency))

        async def verify(item: tuple[dict[str, Any], RunSpec, StagedTrajectory, list[Any]]):
            row, spec, staged, prefix = item
            async with gate:
                return await asyncio.to_thread(
                    evaluate_staged,
                    staged,
                    self.tasks[spec.task_id],
                    verifier=self.verifier,
                    snapshots=self.snapshots,
                    branch_run_id=row["branch_id"],
                    prefix_steps=prefix,
                )

        scored = await asyncio.gather(*[verify(item) for item in pending])
        for (_row, spec, staged, _prefix), (trajectory, evaluation) in zip(pending, scored):
            await self.xp.record_trajectory(trajectory, staged.steps)
            await self.xp.record_evaluation(evaluation)
            results.append(RunResult(spec=spec, trajectory=trajectory, evaluation=evaluation))
        results.sort(key=lambda r: r.spec.index)
        return results

    async def run(self, plan: EvaluationPlan, *, n_workers: int = 1) -> list[RunResult]:
        await self.submit(plan)
        await self.work(plan.run_id, n_workers=n_workers)
        return await self.collect(plan)


# ── metrics ──────────────────────────────────────────────────────────


def metric_value(result: RunResult, metric: str) -> float:
    ev, tr = result.evaluation, result.trajectory
    if metric == "success":
        return 1.0 if (ev.success and not ev.voided and tr.result == "success") else 0.0
    if metric == "regressions":
        return float((ev.audit or {}).get("regressions") or 0)
    if metric == "context_tokens":
        return float(tr.context_tokens or 0)
    value = getattr(ev, metric, None)
    if value is None:
        value = getattr(tr, metric, None)
    return float(value or 0)


def arm_task_values(
    results: list[RunResult], arm: str, metric: str
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for result in results:
        if result.spec.arm == arm:
            out.setdefault(result.spec.task_id, []).append(metric_value(result, metric))
    return out


@dataclass
class ArmAudit:
    runs: int = 0
    voided: int = 0
    tests_modified: int = 0
    timeouts: int = 0
    infra_errors: int = 0
    tokens_incomplete: int = 0
    isolation: set[str] = field(default_factory=set)
    agents: set[str] = field(default_factory=set)
    model_versions: set[str] = field(default_factory=set)
    memory_versions: set[str] = field(default_factory=set)

    def to_json(self) -> dict[str, Any]:
        return {
            "runs": self.runs, "voided": self.voided, "tests_modified": self.tests_modified,
            "timeouts": self.timeouts, "infra_errors": self.infra_errors,
            "tokens_incomplete": self.tokens_incomplete,
            "isolation": sorted(self.isolation), "agents": sorted(self.agents),
            "model_versions": sorted(self.model_versions),
            "memory_versions": sorted(self.memory_versions),
        }


def arm_audit(results: list[RunResult], arm: str) -> ArmAudit:
    audit = ArmAudit()
    for r in results:
        if r.spec.arm != arm:
            continue
        audit.runs += 1
        audit.voided += int(r.evaluation.voided)
        audit.tests_modified += int(bool(r.evaluation.tests_modified))
        audit.timeouts += int(r.trajectory.result == "timeout")
        audit.infra_errors += int(r.trajectory.result == "infra_error")
        audit.tokens_incomplete += int((r.trajectory.metadata or {}).get("tokens_complete") is False)
        audit.isolation.add(r.evaluation.isolation)
        audit.agents.add(r.trajectory.agent)
        audit.model_versions.add(r.trajectory.model_version)
        audit.memory_versions.add(r.trajectory.memory_version)
    return audit
