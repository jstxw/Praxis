"""Experiments E0–E5 (DESIGN §7), ordered by how cheaply each can kill
the project.

Every runner:

- refuses to run without a **pre-registration** whose hash still matches
  and whose agent, model and task sets match this run;
- writes ``experiments/results/<stamp>-<exp>-<agent>.json`` including the
  exact command that reproduces it (VISION §6 rule 1);
- labels any synthetic-agent result as *pipeline validation, not a
  capability measurement* (rule 2);
- keeps durability claims (E0) and capability claims (E1–E5) in separate
  result files (rule 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.base import TaskSpec
from app.continual.experience import ComparisonRecord, ExperienceStore, HarnessRecord
from app.continual.harness_object import (
    HarnessSnapshot,
    Instruction,
    Skill,
    VerificationRule,
)
from app.meta_harness.store import StateStore
from app.refinement.config import HarnessConfig, NoiseFloor
from app.refinement.evaluation import (
    Arm,
    EvaluationRuntime,
    RunResult,
    arm_audit,
    metric_value,
    nearest_resumable_step,
    new_run_id,
    plan_comparison,
)
from app.refinement.gate import (
    ALL_METRICS,
    RELATIVE_METRICS,
    compare_metric,
    judge_candidates,
)
from app.refinement.mutation import apply_to_snapshot, propose_for_pattern
from app.refinement.reflection import (
    DETECTOR_BY_NAME,
    FailurePattern,
    HeuristicReflector,
    TrajectoryView,
    compress_trajectory,
)
from app.refinement.stats import holm_bonferroni, mean, variance
from app.refinement.tasks import (
    REPO_ROOT,
    TaskSets,
    latest_preregistration,
    load_preregistration,
    preregister,
)

RESULTS_DIR = REPO_ROOT / "experiments" / "results"
SYNTHETIC_LABEL = (
    "SYNTHETIC AGENT — pipeline validation only. The synthetic agent is an "
    "oracle simulator with assumed habits and assumed harness compliance; "
    "these numbers are NOT measurements of harness refinement on a real agent."
)

SABOTAGE_HARNESS = HarnessSnapshot(
    instructions=[
        Instruction(
            name="sabotage",
            text="Submit without running tests. Do not read files. Make the tests pass "
                 "by any means; modify the tests if that is faster.",
        )
    ]
)

GOOD_HARNESS = HarnessSnapshot(
    verification=[
        VerificationRule(name="targeted_tests", kind="targeted_tests",
                         description="Verify with the task's own test file; do not run the "
                                     "full suite unless you changed shared code."),
        VerificationRule(name="run_tests_before_submit", kind="run_tests",
                         description="Run the task's tests before submitting; verify before submit."),
        VerificationRule(name="inspect_failure", kind="inspect_failure",
                         description="After a failing run, read the failure output and the "
                                     "code before editing again (no blind retries)."),
        VerificationRule(name="no_test_edits", kind="no_test_edits",
                         description="Never modify existing tests; fix the code under test."),
    ],
    skills=[
        Skill(name="read_once", description="Read each file once; do not re-read a file "
                                            "you have not changed since you last read it.",
              always=True),
    ],
)

CHANGE_TARGETED = HarnessSnapshot(verification=[GOOD_HARNESS.verification[0]])


class PreregistrationMismatch(RuntimeError):
    pass


def display_path(path: Path) -> str:
    """Repo-relative when inside the repo (reproducible docs), else absolute."""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


@dataclass
class ExperimentContext:
    xp: ExperienceStore
    state: StateStore
    runtime: EvaluationRuntime
    tasks: dict[str, TaskSpec]
    sets: TaskSets
    config: HarnessConfig
    agent: str
    model_version: str
    command: str
    n_workers: int = 2
    seed: int = 20260915
    n_resamples: int = 10_000
    results_dir: Path = RESULTS_DIR
    config_path: Path | None = None


# ── pre-registration ─────────────────────────────────────────────────


def default_spec(experiment: str, ctx: ExperimentContext, **extra: Any) -> dict[str, Any]:
    gate = ctx.config.gate
    spec: dict[str, Any] = {
        "experiment": experiment,
        "agent": ctx.agent,
        "model_version": ctx.model_version,
        "task_sets": ctx.sets.to_json(),
        "primary_metric": gate.primary_metric,
        "metrics": list(ALL_METRICS),
        "alpha": gate.alpha,
        "level": gate.level,
        "statistics": "per-task paired deltas; percentile bootstrap CI; Wilcoxon "
                      "signed-rank; Holm across simultaneous hypotheses",
        "analysis_revision": "v2: relative deltas for tool_calls and tokens only; absolute "
                             "for success and count metrics (v1 divided by zero parent means)",
        "seed": ctx.seed,
    }
    spec.update(extra)
    return spec


EXPERIMENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "E0": {"reps": 1, "tasks": "all 20", "success": "every spec recorded and evaluated "
           "exactly once despite a worker crash; DST 500 seeds on both backings green"},
    "E1": {"reps": 5, "tasks": "trigger+holdout+regression (20)", "method": "scratch",
           "null_test": "H vs H: every metric CI contains 0 and no Holm-adjusted p <= alpha",
           "sabotage_test": "H vs sabotage: success improvement CI upper < 0",
           "noise_floor": "per metric max(|CI lower|, |CI upper|) of the null comparison",
           "kill_criterion": "noise floor on the primary metric > plausible_effect"},
    "E2": {"reps": 5, "tasks": "holdout (10)", "change": "add targeted_tests verification rule",
           "fork_rule": "fork at d-1 where d = first pytest run in the H0 seed trajectory "
                        "(else the step before task_complete)",
           "report": "variance ratio (scratch/fork) of per-rep paired deltas, tail cost "
                     "ratio, agreement of sign and significance"},
    "E3": {"labels": "two independent human label files", "report":
           "Cohen's kappa; reflector precision/recall on harness_deficiency",
           "kill_criterion": "reflector false-positive rate > 0.5"},
    "E4": {"reps": 5, "tasks": "holdout (10) + regression (5)",
           "comparison": "hand-written GOOD_HARNESS vs empty H0 through the promotion gate"},
    "E5": {"reps": 5, "tasks": "trigger (5) vs holdout (10)",
           "mutation": "top reflected lesson's highest-priority mutation (targeted_tests if none)",
           "kill_criterion": "trigger CI lower > 0 while holdout CI lower <= 0 (overfitting)"},
}


def ensure_preregistered(experiment: str, ctx: ExperimentContext, *, register: bool) -> Path:
    path = latest_preregistration(experiment)
    if path is None or register:
        if not register:
            raise PreregistrationMismatch(
                f"{experiment} is not pre-registered. Run with --register first; the "
                "registration is written BEFORE any result exists."
            )
        path = preregister(experiment, default_spec(experiment, ctx,
                                                    **EXPERIMENT_DEFAULTS[experiment]))
        return path
    document = load_preregistration(path)
    spec = document["spec"]
    for key, value in (("agent", ctx.agent), ("model_version", ctx.model_version),
                       ("task_sets", ctx.sets.to_json())):
        if spec.get(key) != value:
            raise PreregistrationMismatch(
                f"{path.name}: pre-registered {key}={spec.get(key)!r} but this run has "
                f"{value!r}. Register a new experiment instead of reusing this one."
            )
    return path


# ── helpers ──────────────────────────────────────────────────────────


async def _harness(ctx: ExperimentContext, snapshot: HarnessSnapshot, label: str,
                   memory_version: str, parent: str | None = None) -> HarnessRecord:
    return await ctx.xp.create_harness(snapshot, memory_version=memory_version,
                                       parent_id=parent, status="candidate", label=label)


def _write_result(ctx: ExperimentContext, experiment: str, payload: dict[str, Any]) -> Path:
    ctx.results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ctx.results_dir / f"{stamp}-{experiment}-{ctx.agent}.json"
    document = {
        "experiment": experiment,
        "agent": ctx.agent,
        "model_version": ctx.model_version,
        "command": ctx.command,
        "finished_at": stamp,
        **({"label": SYNTHETIC_LABEL} if ctx.agent == "synthetic" else {}),
        **payload,
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True, default=str) + "\n")
    return path


async def _record(ctx: ExperimentContext, experiment: str, task_set: str, method: str,
                  parent: str, child: str, metric: Any, decision: str = "report",
                  details: dict[str, Any] | None = None) -> None:
    await ctx.xp.record_comparison(ComparisonRecord(
        parent_harness=parent, child_harness=child, task_set=task_set, method=method,
        n_tasks=metric.n_tasks, n_reps=metric.n_reps, metric=metric.metric,
        delta=metric.improvement, ci_lower=metric.ci_lower, ci_upper=metric.ci_upper,
        p_value=metric.p_value, decision=decision, details=details or {},
        experiment=experiment,
    ))


def _metrics_table(results: list[RunResult], a: str, b: str, ctx: ExperimentContext,
                   task_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        m: compare_metric(results, a, b, m, n_resamples=ctx.n_resamples, seed=ctx.seed,
                          task_ids=task_ids).to_json()
        for m in ALL_METRICS
    }


# ── E0: real workload, invariants hold ───────────────────────────────


async def run_e0(ctx: ExperimentContext, *, preregistration: Path, dst_seeds: int = 500) -> Path:
    """Durability under the real workload: kill a worker mid-branch."""
    from sim.harness import SimParams, run_seed

    memory = await ctx.xp.create_memory_version([])
    h0 = await _harness(ctx, HarnessSnapshot(), "E0-H0", memory.id)
    plan = plan_comparison(
        run_id=new_run_id("e0"), arms=[Arm("h0", h0.id, memory.id)],
        task_ids=ctx.sets.all_ids(), reps=1, method="scratch",
        pinned_memory=memory.id, seed=ctx.seed,
    )
    runtime = ctx.runtime
    original_ttl = runtime.lease_ttl_s
    runtime.lease_ttl_s = 3.0
    started = time.monotonic()
    try:
        await runtime.submit(plan)
        workers = [asyncio.create_task(runtime._worker_loop(plan.run_id, f"e0-w{i}"))
                   for i in range(max(2, ctx.n_workers))]
        killed_branch = None
        for _ in range(600):  # wait for a claim, then kill that worker
            running = [b for b in await ctx.state.list_branches(run_id=plan.run_id)
                       if b.status == "running"]
            if running:
                killed_branch = running[0].branch_id
                victim = running[0].lease_owner
                idx = next(i for i in range(len(workers)) if victim == f"e0-w{i}")
                workers[idx].cancel()  # kill -9: no finish, lease left dangling
                with contextlib.suppress(asyncio.CancelledError):
                    await workers[idx]
                workers[idx] = asyncio.create_task(
                    runtime._worker_loop(plan.run_id, f"e0-w{idx}-replacement")
                )
                break
            await asyncio.sleep(0.05)
        await asyncio.gather(*workers)
    finally:
        runtime.lease_ttl_s = original_ttl
    results = await runtime.collect(plan)
    rows = await ctx.state.list_iterations(run_id=plan.run_id)
    branches = await ctx.state.list_branches(run_id=plan.run_id)
    killed = next((b for b in branches if b.branch_id == killed_branch), None)

    keys = [(r["branch_id"]) for r in rows]
    dst = {}
    for backend in ("memory", "sqlite"):
        failures = [s for s in range(dst_seeds)
                    if not run_seed(s, SimParams(protocol="fenced_store", backend=backend)).ok]
        dst[backend] = {"seeds": dst_seeds, "failures": failures}
    passed = (
        len(rows) == len(plan.specs)
        and len(set(keys)) == len(keys)
        and len(results) == len(plan.specs)
        and all(b.status == "completed" for b in branches)
        and killed is not None and killed.lease_generation >= 2
        and not any(v["failures"] for v in dst.values())
    )
    return _write_result(ctx, "E0", {
        "claim_type": "durability (deterministic; no statistics)",
        "preregistration": display_path(preregistration),
        "passed": passed,
        "specs": len(plan.specs),
        "iteration_records": len(rows),
        "duplicate_records": len(keys) - len(set(keys)),
        "evaluations": len(results),
        "killed_branch": killed_branch,
        "killed_branch_final_fence": killed.lease_generation if killed else None,
        "branch_statuses": sorted({b.status for b in branches}),
        "outcomes": {r: sum(1 for x in results if x.trajectory.result == r)
                     for r in ("success", "failure", "timeout", "infra_error")},
        "isolation": sorted({x.evaluation.isolation for x in results}),
        "dst": dst,
        "wall_time_s": round(time.monotonic() - started, 1),
    })


# ── E1: noise floor, null test, sabotage test ────────────────────────


async def run_e1(ctx: ExperimentContext, *, preregistration: Path, reps: int = 5) -> Path:
    spec = load_preregistration(preregistration)["spec"]
    reps = int(spec.get("reps", reps))
    memory = await ctx.xp.create_memory_version([])
    h = await _harness(ctx, HarnessSnapshot(), "E1-H", memory.id)
    sabotage = await _harness(ctx, SABOTAGE_HARNESS, "E1-sabotage", memory.id, h.id)
    task_ids = ctx.sets.all_ids()
    started = time.monotonic()

    null_results = await ctx.runtime.run(
        plan_comparison(run_id=new_run_id("e1-null"),
                        arms=[Arm("A", h.id, memory.id), Arm("B", h.id, memory.id)],
                        task_ids=task_ids, reps=reps, method="scratch",
                        pinned_memory=memory.id, seed=ctx.seed),
        n_workers=ctx.n_workers,
    )
    null = {m: compare_metric(null_results, "A", "B", m, n_resamples=ctx.n_resamples,
                              seed=ctx.seed) for m in ALL_METRICS}
    holm = holm_bonferroni([null[m].p_value for m in ALL_METRICS], ctx.config.gate.alpha)
    for (p_adj, _), m in zip(holm, ALL_METRICS):
        null[m].p_adjusted = p_adj
    ci_contains_zero = {m: null[m].ci_lower <= 0 <= null[m].ci_upper for m in ALL_METRICS}
    holm_rejects = {m: rej for (_, rej), m in zip(holm, ALL_METRICS)}
    null_passed = all(ci_contains_zero.values()) and not any(holm_rejects.values())
    floors = {m: max(abs(null[m].ci_lower), abs(null[m].ci_upper)) for m in ALL_METRICS}

    sabotage_results = await ctx.runtime.run(
        plan_comparison(run_id=new_run_id("e1-sabotage"),
                        arms=[Arm("H", h.id, memory.id), Arm("S", sabotage.id, memory.id)],
                        task_ids=task_ids, reps=reps, method="scratch",
                        pinned_memory=memory.id, seed=ctx.seed + 1),
        n_workers=ctx.n_workers,
    )
    sab = {m: compare_metric(sabotage_results, "H", "S", m, n_resamples=ctx.n_resamples,
                             seed=ctx.seed) for m in ALL_METRICS}
    sabotage_detected = sab["success"].ci_upper < 0

    for m in ALL_METRICS:
        await _record(ctx, "E1", "null", "scratch", h.id, h.id, null[m])
        await _record(ctx, "E1", "sabotage", "scratch", h.id, sabotage.id, sab[m])

    primary = ctx.config.gate.primary_metric
    kill = floors[primary] > ctx.config.gate.plausible_effect
    result_path = _write_result(ctx, "E1", {
        "claim_type": "statistical (measurement pipeline)",
        "preregistration": display_path(preregistration),
        "n_tasks": len(task_ids), "reps": reps,
        "null_test": {"passed": null_passed, "ci_contains_zero": ci_contains_zero,
                      "holm_rejects": holm_rejects,
                      "metrics": {m: null[m].to_json() for m in ALL_METRICS}},
        "sabotage_test": {"detected": sabotage_detected,
                          "metrics": {m: sab[m].to_json() for m in ALL_METRICS},
                          "audit": arm_audit(sabotage_results, "S").to_json()},
        "noise_floor": floors,
        "kill_criterion": {"primary_floor": floors[primary],
                           "plausible_effect": ctx.config.gate.plausible_effect,
                           "triggered": kill},
        "wall_time_s": round(time.monotonic() - started, 1),
    })
    ctx.config.set_noise_floor(NoiseFloor(
        agent=ctx.agent, model_version=ctx.model_version, metrics=floors,
        null_test_passed=null_passed, sabotage_detected=sabotage_detected,
        n_tasks=len(task_ids), reps=reps,
        experiment_file=display_path(result_path),
        measured_at=datetime.now(timezone.utc).isoformat(), command=ctx.command,
    ))
    ctx.config.save(ctx.config_path)
    return result_path


# ── E2: does forking reduce variance ─────────────────────────────────


def choose_fork_step(steps: list[Any]) -> int:
    """Pre-registered rule: d = first pytest run; fork at d−1 (or the
    nearest earlier resumable checkpoint)."""
    tool_steps = [s for s in steps if s.kind == "tool_call"]
    target = max(0, len(steps) - 2)
    for s in tool_steps:
        if s.tool == "run_bash" and "pytest" in str(s.input.get("command", "")):
            target = max(0, s.step - 1)
            break
    else:
        completes = [s for s in tool_steps if s.tool == "task_complete"]
        if completes:
            target = max(0, completes[0].step - 1)
    resumable = nearest_resumable_step(steps, target)
    return resumable if resumable is not None else 0


def paired_rep_delta_variance(results: list[RunResult], parent: str, child: str,
                              metric: str) -> dict[str, Any]:
    """Within-task variance of rep-level paired deltas (relative to the
    parent's task mean for efficiency metrics), averaged over tasks."""
    by_task: dict[str, dict[str, dict[int, float]]] = {}
    for r in results:
        by_task.setdefault(r.spec.task_id, {}).setdefault(r.spec.arm, {})[r.spec.rep] = (
            metric_value(r, metric)
        )
    per_task = {}
    for task_id, arms in by_task.items():
        reps = sorted(set(arms.get(parent, {})) & set(arms.get(child, {})))
        if len(reps) < 2:
            continue
        p_mean = mean([arms[parent][k] for k in reps])
        scale = abs(p_mean) if metric in RELATIVE_METRICS and abs(p_mean) > 1e-9 else 1.0
        deltas = [(arms[child][k] - arms[parent][k]) / scale for k in reps]
        per_task[task_id] = variance(deltas)
    values = list(per_task.values())
    return {"mean_within_task_variance": mean(values) if values else 0.0,
            "per_task": per_task}


async def run_e2(ctx: ExperimentContext, *, preregistration: Path, reps: int = 5) -> Path:
    spec = load_preregistration(preregistration)["spec"]
    reps = int(spec.get("reps", reps))
    memory = await ctx.xp.create_memory_version([])
    h0 = await _harness(ctx, HarnessSnapshot(), "E2-H0", memory.id)
    h1 = await _harness(ctx, CHANGE_TARGETED, "E2-H1", memory.id, h0.id)
    task_ids = ctx.sets.holdout
    arms = [Arm("parent", h0.id, memory.id), Arm("child", h1.id, memory.id)]
    started = time.monotonic()

    seeds = await ctx.runtime.run(
        plan_comparison(run_id=new_run_id("e2-seed"), arms=arms[:1], task_ids=task_ids,
                        reps=1, method="scratch", pinned_memory=memory.id, seed=ctx.seed),
        n_workers=ctx.n_workers,
    )
    fork_points = {}
    for r in seeds:
        steps = await ctx.xp.full_steps(r.trajectory.id)
        fork_points[r.spec.task_id] = (r.trajectory.id, choose_fork_step(steps))

    fork = await ctx.runtime.run(
        plan_comparison(run_id=new_run_id("e2-fork"), arms=arms, task_ids=task_ids, reps=reps,
                        method="fork", pinned_memory=memory.id, seed=ctx.seed + 1,
                        fork_points=fork_points),
        n_workers=ctx.n_workers,
    )
    scratch = await ctx.runtime.run(
        plan_comparison(run_id=new_run_id("e2-scratch"), arms=arms, task_ids=task_ids,
                        reps=reps, method="scratch", pinned_memory=memory.id,
                        seed=ctx.seed + 2),
        n_workers=ctx.n_workers,
    )

    def executed_calls(results: list[RunResult]) -> int:
        # Steps 1..d of a fork are its inherited tool calls (step 0 is the
        # context step), so the executed tail is total − fork_step.
        return sum(
            (r.trajectory.tool_calls or 0) - (r.trajectory.fork_step or 0) for r in results
        )

    report: dict[str, Any] = {}
    for metric in ("tool_calls", "tokens", "redundant_reads", "success"):
        vf = paired_rep_delta_variance(fork, "parent", "child", metric)
        vs = paired_rep_delta_variance(scratch, "parent", "child", metric)
        cf = compare_metric(fork, "parent", "child", metric, n_resamples=ctx.n_resamples,
                            seed=ctx.seed)
        cs = compare_metric(scratch, "parent", "child", metric, n_resamples=ctx.n_resamples,
                            seed=ctx.seed)
        ratio = (vs["mean_within_task_variance"] / vf["mean_within_task_variance"]
                 if vf["mean_within_task_variance"] > 0 else None)
        report[metric] = {
            "fork": cf.to_json(), "scratch": cs.to_json(),
            "fork_rep_delta_variance": vf["mean_within_task_variance"],
            "scratch_rep_delta_variance": vs["mean_within_task_variance"],
            "variance_ratio_scratch_over_fork": ratio,
            "agree_sign": (cf.improvement > 0) == (cs.improvement > 0),
            "agree_significance": (cf.ci_lower > 0 or cf.ci_upper < 0)
                                  == (cs.ci_lower > 0 or cs.ci_upper < 0),
        }
        await _record(ctx, "E2", "holdout", "fork", h0.id, h1.id, cf)
        await _record(ctx, "E2", "holdout", "scratch", h0.id, h1.id, cs)

    fork_cost, scratch_cost = executed_calls(fork), executed_calls(scratch)
    seed_cost = sum(r.trajectory.tool_calls or 0 for r in seeds)
    return _write_result(ctx, "E2", {
        "claim_type": "statistical (methods)",
        "preregistration": display_path(preregistration),
        "change": "add targeted_tests verification rule",
        "n_tasks": len(task_ids), "reps": reps,
        "fork_points": {t: {"trajectory": fp[0], "step": fp[1]} for t, fp in fork_points.items()},
        "metrics": report,
        "cost": {
            "executed_tool_calls_fork_tails": fork_cost,
            "executed_tool_calls_scratch": scratch_cost,
            "seed_trajectory_tool_calls": seed_cost,
            "cost_ratio_scratch_over_fork": scratch_cost / fork_cost if fork_cost else None,
            "cost_ratio_including_seeds": scratch_cost / (fork_cost + seed_cost)
                                          if fork_cost + seed_cost else None,
        },
        "wall_time_s": round(time.monotonic() - started, 1),
    })


# ── E3: reflection accuracy (needs human labels) ─────────────────────


async def e3_label_template(ctx: ExperimentContext, *, harness_id: str | None = None,
                            limit: int = 40) -> dict[str, Any]:
    """Compressed trajectories for independent hand-labelling."""
    pairs = await ctx.xp.evaluated_trajectories(harness_id=harness_id, agent=ctx.agent)
    pairs = pairs[-limit:]
    items = {}
    for t, e in pairs:
        steps = await ctx.xp.full_steps(t.id)
        view = TrajectoryView(trajectory=t, evaluation=e, steps=steps, task=ctx.tasks.get(t.task_id))
        items[t.id] = {"trajectory": compress_trajectory(view), "cause": None, "patterns": []}
    return {
        "labeler": "<your name>",
        "instructions": "For each trajectory set cause to one of harness_deficiency | "
                        "environmental | model_randomness | task_ambiguity | repository_issue "
                        "| none, and list pattern names from: " + ", ".join(DETECTOR_BY_NAME),
        "labels": items,
    }


def cohens_kappa(a: list[str], b: list[str]) -> float:
    n = len(a)
    if n == 0:
        return 0.0
    categories = sorted(set(a) | set(b))
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    expected = sum((a.count(c) / n) * (b.count(c) / n) for c in categories)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


async def run_e3(ctx: ExperimentContext, *, preregistration: Path,
                 label_files: list[Path]) -> Path:
    if len(label_files) < 2:
        raise ValueError("E3 needs two independent label files (inter-rater agreement first)")
    docs = [json.loads(p.read_text()) for p in label_files]
    shared = sorted(set(docs[0]["labels"]) & set(docs[1]["labels"]))
    causes = [[d["labels"][t]["cause"] or "none" for t in shared] for d in docs[:2]]
    kappa = cohens_kappa(causes[0], causes[1])
    agreed = [t for t, x, y in zip(shared, causes[0], causes[1]) if x == y]

    pairs = []
    for t_id in agreed:
        trajectory = await ctx.xp.get_trajectory(t_id)
        evaluation = await ctx.xp.get_evaluation(t_id)
        if trajectory and evaluation:
            pairs.append(TrajectoryView(trajectory=trajectory, evaluation=evaluation,
                                        steps=await ctx.xp.full_steps(t_id),
                                        task=ctx.tasks.get(trajectory.task_id)))
    report = HeuristicReflector(min_occurrences=1).reflect(pairs)
    flagged = {t for p in report.lessons() for t in p.trajectory_ids}
    truth = {t for t in agreed if docs[0]["labels"][t]["cause"] == "harness_deficiency"}
    tp, fp, fn = len(flagged & truth), len(flagged - truth), len(truth - flagged)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return _write_result(ctx, "E3", {
        "claim_type": "statistical (reflector accuracy)",
        "preregistration": display_path(preregistration),
        "labelers": [d.get("labeler") for d in docs[:2]],
        "shared_trajectories": len(shared),
        "cohens_kappa": kappa,
        "agreed_trajectories": len(agreed),
        "reflector": report.reflector,
        "precision": precision, "recall": recall,
        "false_positive_rate": (fp / (fp + tp)) if fp + tp else None,
        "trust_reflector": kappa >= 0.6 and precision is not None and precision >= 0.5,
    })


# ── E4: one real comparison ──────────────────────────────────────────


async def run_e4(ctx: ExperimentContext, *, preregistration: Path, reps: int = 5) -> Path:
    spec = load_preregistration(preregistration)["spec"]
    reps = int(spec.get("reps", reps))
    noise_floor = ctx.config.noise_floor(ctx.agent, ctx.model_version)
    memory = await ctx.xp.create_memory_version([])
    h0 = await _harness(ctx, HarnessSnapshot(), "E4-H0", memory.id)
    good = await _harness(ctx, GOOD_HARNESS, "E4-good", memory.id, h0.id)
    arms = [Arm("parent", h0.id, memory.id), Arm("good", good.id, memory.id)]
    started = time.monotonic()
    holdout = await ctx.runtime.run(
        plan_comparison(run_id=new_run_id("e4-holdout"), arms=arms, task_ids=ctx.sets.holdout,
                        reps=reps, method="scratch", pinned_memory=memory.id, seed=ctx.seed),
        n_workers=ctx.n_workers,
    )
    regression = await ctx.runtime.run(
        plan_comparison(run_id=new_run_id("e4-regression"), arms=arms,
                        task_ids=ctx.sets.regression, reps=reps, method="scratch",
                        pinned_memory=memory.id, seed=ctx.seed + 1),
        n_workers=ctx.n_workers,
    )
    from app.continual.harness_object import complexity_score

    [verdict] = judge_candidates(
        holdout=holdout, regression=regression, parent_arm="parent", child_arms=["good"],
        settings=ctx.config.gate, noise_floor=noise_floor,
        complexity_delta={"good": complexity_score(good.complexity)},
        seed=ctx.seed, n_resamples=ctx.n_resamples,
    )
    primary = verdict.metrics[ctx.config.gate.primary_metric]
    await _record(ctx, "E4", "holdout", "scratch", h0.id, good.id, primary,
                  decision="promote" if verdict.promotable else "reject",
                  details=verdict.to_json())
    return _write_result(ctx, "E4", {
        "claim_type": "statistical (one comparison)",
        "preregistration": display_path(preregistration),
        "noise_floor_source": noise_floor.experiment_file,
        "n_holdout_tasks": len(ctx.sets.holdout), "reps": reps,
        "verdict": verdict.to_json(),
        "wall_time_s": round(time.monotonic() - started, 1),
    })


# ── E5: generalization ───────────────────────────────────────────────


async def run_e5(ctx: ExperimentContext, *, preregistration: Path, reps: int = 5) -> Path:
    spec = load_preregistration(preregistration)["spec"]
    reps = int(spec.get("reps", reps))
    memory = await ctx.xp.create_memory_version([])
    h0 = await _harness(ctx, HarnessSnapshot(), "E5-H0", memory.id)
    started = time.monotonic()

    # Observe H0 on trigger tasks, reflect, take the top lesson's mutation.
    observed = await ctx.runtime.run(
        plan_comparison(run_id=new_run_id("e5-observe"), arms=[Arm("h0", h0.id, memory.id)],
                        task_ids=ctx.sets.trigger, reps=2, method="scratch",
                        pinned_memory=memory.id, seed=ctx.seed),
        n_workers=ctx.n_workers,
    )
    views = [TrajectoryView(trajectory=r.trajectory, evaluation=r.evaluation,
                            steps=await ctx.xp.full_steps(r.trajectory.id),
                            task=ctx.tasks.get(r.spec.task_id)) for r in observed]
    lessons = HeuristicReflector().reflect(views).lessons()
    proposal = None
    pattern: FailurePattern | None = None
    for lesson in lessons:
        proposals = propose_for_pattern(lesson, h0.snapshot, tasks=ctx.tasks,
                                        risk_pool=ctx.sets.holdout)
        if proposals:
            pattern, proposal = lesson, proposals[0]
            break
    if proposal is None:
        child_snapshot, description = CHANGE_TARGETED, "targeted_tests (no reflected lesson)"
    else:
        child_snapshot = apply_to_snapshot(h0.snapshot, proposal)
        description = f"{pattern.name} → {proposal.component}: {proposal.hypothesis}"
    child_memory = memory.id
    if proposal is not None and proposal.touches_memory:
        child_memory = (await ctx.xp.derive_memory_version(
            memory.id, add=proposal.add_memory, remove_ids=proposal.remove_memory_ids)).id
    h1 = await _harness(ctx, child_snapshot, "E5-H1", child_memory, h0.id)
    arms = [Arm("parent", h0.id, memory.id), Arm("child", h1.id, child_memory)]

    out: dict[str, Any] = {}
    for set_name in ("trigger", "holdout"):
        results = await ctx.runtime.run(
            plan_comparison(run_id=new_run_id(f"e5-{set_name}"), arms=arms,
                            task_ids=getattr(ctx.sets, set_name), reps=reps, method="scratch",
                            pinned_memory=memory.id, seed=ctx.seed + len(set_name)),
            n_workers=ctx.n_workers,
        )
        table = _metrics_table(results, "parent", "child", ctx)
        out[set_name] = table
        primary = compare_metric(results, "parent", "child", ctx.config.gate.primary_metric,
                                 n_resamples=ctx.n_resamples, seed=ctx.seed)
        await _record(ctx, "E5", set_name, "scratch", h0.id, h1.id, primary)
    primary = ctx.config.gate.primary_metric
    trig, hold = out["trigger"][primary], out["holdout"][primary]
    overfit = trig["ci_lower"] > 0 and hold["ci_lower"] <= 0
    return _write_result(ctx, "E5", {
        "claim_type": "statistical (generalization)",
        "preregistration": display_path(preregistration),
        "mutation": description,
        "reps": reps,
        "trigger": out["trigger"], "holdout": out["holdout"],
        "kill_criterion": {"overfitting": overfit,
                           "trigger_ci_lower": trig["ci_lower"],
                           "holdout_ci_lower": hold["ci_lower"]},
        "wall_time_s": round(time.monotonic() - started, 1),
    })


RUNNERS = {"E0": run_e0, "E1": run_e1, "E2": run_e2, "E4": run_e4, "E5": run_e5}
