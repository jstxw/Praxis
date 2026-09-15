"""The refinement loop (ARCHITECTURE §3; DESIGN §8 Phase 6).

    CODING TASK → ACTIVE HARNESS → AGENT → TRAJECTORY → TASK EVALUATION
      → EXPERIENCE STORE → REFINEMENT POLICY
          ├── no evidence ─► continue
          └── justified ─► REFLECTION (what, which component, which step)
                ─► MUTATION HYPOTHESES (branch factor 3)
                ─► TARGETED EVALUATION on trigger tasks (cheap; forks at d−1)
                ─► COMPARE TO PARENT on holdout + regression (the gate)
                ─► reject | promote (update_frontier)

Rules this module enforces rather than documents:

- trigger-task results screen candidates but **never** promote one;
  promotion is decided on the holdout set (regression to the mean);
- every comparison pins one memory version for all arms;
- the noise floor must be measured (E1) before any promotion;
- each cycle stops at its wall-clock budget;
- every ``promote_every_n`` promotions the active harness is re-tested
  against the original ``H0``, not just its parent, to catch chains of
  noise promotions (DESIGN §10).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.agents.base import TaskSpec
from app.continual.experience import (
    ComparisonRecord,
    ExperienceStore,
    FailurePatternRecord,
    HarnessEventRecord,
    HarnessRecord,
    MutationRecord,
    ProjectRecord,
    ReflectionRecord,
)
from app.refinement.config import HarnessConfig, NoiseFloorUnmeasured
from app.refinement.evaluation import (
    Arm,
    EvaluationRuntime,
    RunResult,
    new_run_id,
    plan_comparison,
)
from app.refinement.gate import (
    CandidateVerdict,
    compare_metric,
    judge_candidates,
    select_promotion,
)
from app.refinement.mutation import MutationProposal, apply_to_snapshot, propose
from app.refinement.policy import WORK_RUN_PREFIX, compute_stats, should_reflect
from app.refinement.reflection import FailurePattern, HeuristicReflector, TrajectoryView
from app.refinement.tasks import TaskSets


@dataclass
class CycleOutcome:
    status: str  # no_evidence | no_lesson | no_candidates | rejected | promoted | budget_exhausted | no_noise_floor
    reason: str
    promoted: str | None = None
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    screened: dict[str, float] = field(default_factory=dict)
    reflection_id: str | None = None
    wall_time_s: float = 0.0


@dataclass
class Candidate:
    arm: str
    proposal: MutationProposal
    pattern: FailurePattern | None
    harness: HarnessRecord
    memory_version: str


class RefinementLoop:
    def __init__(
        self,
        *,
        xp: ExperienceStore,
        runtime: EvaluationRuntime,
        tasks: dict[str, TaskSpec],
        task_sets: TaskSets,
        config: HarnessConfig,
        agent: str,
        model_version: str,
        reflector: Any | None = None,
        n_workers: int = 1,
        seed: int = 0,
        n_resamples: int = 10_000,
    ) -> None:
        self.xp = xp
        self.runtime = runtime
        self.tasks = tasks
        self.sets = task_sets
        self.config = config
        self.agent = agent
        self.model_version = model_version
        self.reflector = reflector or HeuristicReflector()
        self.n_workers = n_workers
        self.seed = seed
        self.n_resamples = n_resamples

    # ── observed work ────────────────────────────────────────────────

    async def run_task(self, project: ProjectRecord, task_id: str, *, seed: int) -> RunResult:
        """Run one real task under the active harness (observed work)."""
        arm = Arm("active", project.active_harness, project.memory_version)
        plan = plan_comparison(
            run_id=new_run_id("work"),  # "hx-work-…" == WORK_RUN_PREFIX + hex
            arms=[arm], task_ids=[task_id], reps=1, method="scratch",
            pinned_memory=project.memory_version, seed=seed,
        )
        [result] = await self.runtime.run(plan, n_workers=1)
        return result

    # ── one cycle ────────────────────────────────────────────────────

    async def cycle(self, project: ProjectRecord) -> CycleOutcome:
        started = time.monotonic()
        budget_s = self.config.budget.max_wall_clock_minutes * 60

        def over_budget() -> bool:
            return time.monotonic() - started > budget_s

        try:
            noise_floor = self.config.noise_floor(self.agent, self.model_version)
        except NoiseFloorUnmeasured as exc:
            noise_floor = None
            floor_error = str(exc)
        else:
            floor_error = ""

        reflections = await self.xp.list_reflections(project_id=project.id)
        last_at = reflections[-1].created_at if reflections else None
        stats = await compute_stats(
            self.xp, harness_id=project.active_harness, agent=self.agent,
            last_reflection_at=last_at, tasks=self.tasks,
        )
        decision = should_reflect(
            stats, noise_floor_success=noise_floor.floor("success") if noise_floor else None
        )
        if not decision.reflect:
            return CycleOutcome("no_evidence", decision.reason, wall_time_s=_since(started))

        # ── reflection ──
        views = await self._recent_views(project)
        report = self.reflector.reflect(views)
        reflection = ReflectionRecord(
            project_id=project.id, harness_id=project.active_harness,
            trigger=decision.reason, reflector=report.reflector,
            report=report.model_dump(mode="json"),
        )
        await self.xp.record_reflection(reflection)
        pattern_ids: dict[str, str] = {}
        for p in report.failure_patterns:
            record = FailurePatternRecord(
                name=p.name, description=p.description, trajectory_ids=p.trajectory_ids,
                divergence_steps=p.divergence_steps, affected_component=p.affected_component,
                scope=p.scope, classification=p.classification, reflection_id=reflection.id,
            )
            await self.xp.record_failure_pattern(record)
            pattern_ids[p.name] = record.id
        lessons = report.lessons()
        if not lessons:
            return CycleOutcome("no_lesson", "reflection found no harness deficiency",
                                reflection_id=reflection.id, wall_time_s=_since(started))
        if noise_floor is None:
            # Reflection is recorded; nothing is evaluated or promoted
            # until E1 has measured the floor for this agent/model.
            return CycleOutcome("no_noise_floor", floor_error, reflection_id=reflection.id,
                                wall_time_s=_since(started))

        # ── mutation hypotheses ──
        parent = await self.xp.get_harness(project.active_harness)
        assert parent is not None
        proposals = propose(
            lessons, parent.snapshot, tasks=self.tasks,
            risk_pool=self.sets.regression + self.sets.holdout,
            branch_factor=self.config.budget.branch_factor,
        )
        if not proposals:
            return CycleOutcome("no_candidates", "no applicable mutation templates",
                                reflection_id=reflection.id, wall_time_s=_since(started))
        candidates = await self._materialize(project, parent, proposals, pattern_ids)

        # ── stage A: cheap targeted screen on trigger tasks ──
        top = lessons[0]
        screened = await self._screen(project, parent, candidates, top)
        if over_budget():
            return CycleOutcome("budget_exhausted", "wall-clock budget hit after screening",
                                screened=screened, reflection_id=reflection.id,
                                wall_time_s=_since(started))
        ranked = sorted(candidates, key=lambda c: -screened.get(c.arm, 0.0))
        survivors = [c for c in ranked if screened.get(c.arm, 0.0) > 0][
            : self.config.budget.survivors
        ]
        if not survivors:
            await self._reject_all(project, parent, candidates, "no candidate improved on trigger tasks")
            return CycleOutcome("rejected", "no candidate improved on the trigger screen",
                                screened=screened, reflection_id=reflection.id,
                                wall_time_s=_since(started))

        # ── stage B: holdout + regression, the promotion criterion ──
        arms = [Arm("parent", parent.id, project.memory_version)] + [
            Arm(c.arm, c.harness.id, c.memory_version) for c in survivors
        ]
        for c in survivors:
            await self.xp.set_harness_status(c.harness.id, "evaluating")
        holdout_ids = self.sets.holdout[: self.config.budget.holdout_tasks]
        regression_ids = self.sets.regression[: self.config.budget.regression_tasks]
        reps = self.config.budget.reps
        holdout = await self.runtime.run(
            plan_comparison(run_id=new_run_id("holdout"), arms=arms, task_ids=holdout_ids,
                            reps=reps, method="scratch", pinned_memory=project.memory_version,
                            seed=self.seed + 1),
            n_workers=self.n_workers,
        )
        regression = await self.runtime.run(
            plan_comparison(run_id=new_run_id("regression"), arms=arms, task_ids=regression_ids,
                            reps=max(2, reps // 2), method="scratch",
                            pinned_memory=project.memory_version, seed=self.seed + 2),
            n_workers=self.n_workers,
        ) if regression_ids else []

        complexity_delta = {
            c.arm: _complexity(c.harness) - _complexity(parent) for c in survivors
        }
        verdicts = judge_candidates(
            holdout=holdout, regression=regression, parent_arm="parent",
            child_arms=[c.arm for c in survivors], settings=self.config.gate,
            noise_floor=noise_floor, complexity_delta=complexity_delta,
            seed=self.seed, n_resamples=self.n_resamples,
        )
        winner = select_promotion(verdicts)
        by_arm = {c.arm: c for c in survivors}
        for verdict in verdicts:
            await self._record_verdict(parent, by_arm[verdict.arm], verdict, winner)

        for c in candidates:
            if winner is None or c.arm != winner.arm:
                await self.xp.set_harness_status(c.harness.id, "rejected")
        if winner is None:
            await self.xp.record_harness_event(HarnessEventRecord(
                project_id=project.id, kind="reject", harness_id=parent.id,
                summary=f"{len(verdicts)} candidate(s) rejected for {top.name}",
                details={"verdicts": [v.to_json() for v in verdicts]},
            ))
            return CycleOutcome("rejected", "no candidate cleared the gate",
                                verdicts=[v.to_json() for v in verdicts], screened=screened,
                                reflection_id=reflection.id, wall_time_s=_since(started))

        chosen = by_arm[winner.arm]
        await self._promote(project, parent, chosen, winner, top)
        return CycleOutcome("promoted", f"{parent.label or parent.id[:8]} → "
                            f"{chosen.harness.label or chosen.harness.id[:8]}",
                            promoted=chosen.harness.id,
                            verdicts=[v.to_json() for v in verdicts], screened=screened,
                            reflection_id=reflection.id, wall_time_s=_since(started))

    # ── helpers ──────────────────────────────────────────────────────

    async def _recent_views(self, project: ProjectRecord, limit: int = 40) -> list[TrajectoryView]:
        from app.refinement.policy import is_observed_work

        pairs = await self.xp.evaluated_trajectories(harness_id=project.active_harness,
                                                     agent=self.agent)
        pairs = [(t, e) for t, e in pairs if is_observed_work(t)][-limit:]
        views = []
        for t, e in pairs:
            views.append(TrajectoryView(trajectory=t, evaluation=e,
                                        steps=await self.xp.full_steps(t.id),
                                        task=self.tasks.get(t.task_id)))
        return views

    async def _materialize(
        self,
        project: ProjectRecord,
        parent: HarnessRecord,
        proposals: list[tuple[FailurePattern | None, MutationProposal]],
        pattern_ids: dict[str, str],
    ) -> list[Candidate]:
        lineage = await self.xp.lineage(parent.id)
        generation = len(lineage)
        candidates = []
        for i, (pattern, proposal) in enumerate(proposals):
            memory_version = project.memory_version
            if proposal.touches_memory:
                derived = await self.xp.derive_memory_version(
                    project.memory_version, add=proposal.add_memory,
                    remove_ids=proposal.remove_memory_ids,
                )
                memory_version = derived.id
            child = await self.xp.create_harness(
                apply_to_snapshot(parent.snapshot, proposal),
                memory_version=memory_version, parent_id=parent.id, status="candidate",
                label=f"H{generation}.{chr(ord('a') + i)}",
            )
            await self.xp.record_mutation(MutationRecord(
                parent_harness=parent.id, child_harness=child.id,
                pattern_id=pattern_ids.get(pattern.name, "deletion") if pattern else "deletion",
                hypothesis=proposal.hypothesis, predicted_fix=proposal.predicted_fix,
                predicted_risk=proposal.predicted_risk, payload=proposal.payload(),
            ))
            candidates.append(Candidate(arm=f"cand{i}", proposal=proposal, pattern=pattern,
                                        harness=child, memory_version=memory_version))
        return candidates

    async def _screen(
        self,
        project: ProjectRecord,
        parent: HarnessRecord,
        candidates: list[Candidate],
        pattern: FailurePattern,
    ) -> dict[str, float]:
        """Primary-metric improvement on trigger tasks. Fork-based for local
        patterns (fork at d−1 of the triggering trajectories); scratch for
        global ones. Recorded as ``task_set=trigger`` — never a promotion."""
        arms = [Arm("parent", parent.id, project.memory_version)] + [
            Arm(c.arm, c.harness.id, c.memory_version) for c in candidates
        ]
        fork_points: dict[str, tuple[str, int]] = {}
        for trajectory_id, step in pattern.fork_points():
            trajectory = await self.xp.get_trajectory(trajectory_id)
            if trajectory and trajectory.task_id not in fork_points and trajectory.task_id in self.tasks:
                fork_points[trajectory.task_id] = (trajectory_id, step)
        task_ids = list(fork_points)[: self.config.budget.cheap_eval_tasks]
        method = "fork" if pattern.scope == "local" and self.runtime.adapter_factory().supports_fork else "scratch"
        if not task_ids:
            task_ids = list(pattern.task_ids)[: self.config.budget.cheap_eval_tasks]
            method = "scratch"
        plan = plan_comparison(
            run_id=new_run_id("trigger"), arms=arms, task_ids=task_ids, reps=2,
            method=method, pinned_memory=project.memory_version, seed=self.seed,
            fork_points=fork_points if method == "fork" else None,
        )
        results = await self.runtime.run(plan, n_workers=self.n_workers)
        screened: dict[str, float] = {}
        primary = self.config.gate.primary_metric
        for c in candidates:
            metric = compare_metric(results, "parent", c.arm, primary,
                                    n_resamples=min(self.n_resamples, 2000), seed=self.seed)
            success = compare_metric(results, "parent", c.arm, "success",
                                     n_resamples=min(self.n_resamples, 2000), seed=self.seed)
            # A candidate that loses success on its own trigger tasks is out.
            screened[c.arm] = metric.improvement if success.improvement >= 0 else -1.0
            await self.xp.record_comparison(ComparisonRecord(
                parent_harness=parent.id, child_harness=c.harness.id, task_set="trigger",
                method=method, n_tasks=metric.n_tasks, n_reps=metric.n_reps, metric=primary,
                delta=metric.improvement, ci_lower=metric.ci_lower, ci_upper=metric.ci_upper,
                p_value=metric.p_value, decision="report",
                details={
                    "note": "trigger-set screen; never a promotion criterion",
                    "success_improvement": success.improvement,
                    "predictions": _score_predictions(results, c, primary),
                    "pattern": pattern.name,
                },
            ))
        return screened

    async def _record_verdict(
        self,
        parent: HarnessRecord,
        candidate: Candidate,
        verdict: CandidateVerdict,
        winner: CandidateVerdict | None,
    ) -> None:
        primary = self.config.gate.primary_metric
        pm = verdict.metrics[primary]
        promoted = winner is not None and winner.arm == verdict.arm
        await self.xp.record_comparison(ComparisonRecord(
            parent_harness=parent.id, child_harness=candidate.harness.id,
            task_set="holdout", method="scratch", n_tasks=pm.n_tasks, n_reps=pm.n_reps,
            metric=primary, delta=pm.improvement, ci_lower=pm.ci_lower, ci_upper=pm.ci_upper,
            p_value=pm.p_adjusted if pm.p_adjusted is not None else pm.p_value,
            decision="promote" if promoted else "reject",
            details={**verdict.to_json(), "hypothesis": candidate.proposal.hypothesis,
                     "component": candidate.proposal.component,
                     "pattern": candidate.pattern.name if candidate.pattern else "deletion"},
        ))

    async def _reject_all(self, project, parent, candidates, reason: str) -> None:
        for c in candidates:
            await self.xp.set_harness_status(c.harness.id, "rejected")
        await self.xp.record_harness_event(HarnessEventRecord(
            project_id=project.id, kind="reject", harness_id=parent.id, summary=reason,
        ))

    async def _promote(
        self,
        project: ProjectRecord,
        parent: HarnessRecord,
        chosen: Candidate,
        verdict: CandidateVerdict,
        pattern: FailurePattern,
    ) -> None:
        """``update_frontier``: the candidate becomes the active harness."""
        primary = self.config.gate.primary_metric
        pm = verdict.metrics[primary]
        await self.xp.set_harness_status(parent.id, "archived")
        await self.xp.set_harness_status(chosen.harness.id, "active")
        await self.xp.update_project(project.id, active_harness=chosen.harness.id,
                                     memory_version=chosen.memory_version)
        n_occ, n_views = pattern.occurrence_count, len(set(pattern.task_ids))
        await self.xp.record_harness_event(HarnessEventRecord(
            project_id=project.id, kind="promote", harness_id=chosen.harness.id,
            from_harness=parent.id,
            summary=(
                f"{pattern.description} in {n_occ} trajectories across {n_views} tasks. "
                f"{chosen.proposal.hypothesis} Holdout: {primary} "
                f"{pm.improvement:+.1%} (CI {pm.ci_lower:+.1%}..{pm.ci_upper:+.1%}); "
                f"success {verdict.metrics['success'].improvement:+.1%}."
            ),
            details={
                "evidence": pattern.model_dump(mode="json"),
                "hypothesis": chosen.proposal.hypothesis,
                "mutation": chosen.proposal.payload(),
                "evaluation": verdict.to_json(),
            },
        ))
        promotions = await self.xp.list_harness_events(project.id, kind="promote")
        if len(promotions) % max(1, self.config.gate.drift_retest_every) == 0:
            await self.drift_check(project)

    async def drift_check(self, project: ProjectRecord) -> dict[str, Any]:
        """Re-test the active harness against the original H0 (DESIGN §10)."""
        project = await self.xp.get_project(project_id=project.id) or project
        arms = [Arm("h0", project.h0_harness, project.memory_version),
                Arm("active", project.active_harness, project.memory_version)]
        results = await self.runtime.run(
            plan_comparison(run_id=new_run_id("drift"), arms=arms,
                            task_ids=self.sets.holdout[: self.config.budget.holdout_tasks],
                            reps=self.config.budget.reps, method="scratch",
                            pinned_memory=project.memory_version, seed=self.seed + 7),
            n_workers=self.n_workers,
        )
        primary = self.config.gate.primary_metric
        pm = compare_metric(results, "h0", "active", primary, n_resamples=self.n_resamples,
                            seed=self.seed)
        promotions = await self.xp.list_harness_events(project.id, kind="promote")
        claimed = 0.0
        for event in promotions:
            try:
                claimed += event.details["evaluation"]["metrics"][primary]["improvement"]
            except (KeyError, TypeError):
                pass
        drifted = pm.ci_upper < claimed  # compounding: intermediate promotions were noise
        details = {"metric": pm.to_json(), "claimed_sum": claimed, "drifted": drifted}
        await self.xp.record_comparison(ComparisonRecord(
            parent_harness=project.h0_harness, child_harness=project.active_harness,
            task_set="drift", method="scratch", n_tasks=pm.n_tasks, n_reps=pm.n_reps,
            metric=primary, delta=pm.improvement, ci_lower=pm.ci_lower, ci_upper=pm.ci_upper,
            p_value=pm.p_value, decision="report", details=details,
        ))
        await self.xp.record_harness_event(HarnessEventRecord(
            project_id=project.id, kind="drift_check", harness_id=project.active_harness,
            from_harness=project.h0_harness,
            summary=(f"active vs H0: {primary} {pm.improvement:+.1%} "
                     f"(CI upper {pm.ci_upper:+.1%}) vs claimed sum {claimed:+.1%}"
                     + (" — DRIFT: chain gain below claimed deltas" if drifted else "")),
            details=details,
        ))
        return details


def _complexity(harness: HarnessRecord) -> float:
    from app.continual.harness_object import complexity_score

    return complexity_score(harness.complexity)


def _score_predictions(results: list[RunResult], candidate: Candidate, metric: str) -> dict[str, Any]:
    """Falsifiable contract: did the predicted-fix tasks improve?"""
    from app.refinement.evaluation import arm_task_values

    parent = arm_task_values(results, "parent", metric)
    child = arm_task_values(results, candidate.arm, metric)
    hits, checked = [], []
    for task_id in candidate.proposal.predicted_fix:
        if task_id in parent and task_id in child:
            checked.append(task_id)
            p = sum(parent[task_id]) / len(parent[task_id])
            c = sum(child[task_id]) / len(child[task_id])
            if c < p:
                hits.append(task_id)
    return {"predicted_fix_checked": checked, "predicted_fix_hits": hits,
            "predicted_risk": candidate.proposal.predicted_risk,
            "risk_scored": False}


def _since(started: float) -> float:
    return round(time.monotonic() - started, 2)
