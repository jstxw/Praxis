"""One full refinement cycle, end to end, on the synthetic agent.

Mechanics only: observed work → policy → reflection with step
attribution → three candidates → fork-based trigger screen → holdout +
regression gate → promote or reject, with every artifact recorded. The
noise floor here is a TEST FIXTURE, not a measurement, and the budget is
scaled down so the cycle runs in seconds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agents.synthetic import SyntheticAgent  # noqa: E402
from app.continual.experience import ExperienceStore, ProjectRecord  # noqa: E402
from app.continual.harness_object import HarnessSnapshot  # noqa: E402
from app.continual.snapshots import SnapshotStore  # noqa: E402
from app.meta_harness.sqlite_store import SQLiteStateStore  # noqa: E402
from app.refinement.config import (  # noqa: E402
    GateSettings,
    HarnessConfig,
    NoiseFloor,
    RefinementBudget,
)
from app.refinement.evaluation import EvaluationRuntime  # noqa: E402
from app.refinement.evaluator import Verifier  # noqa: E402
from app.refinement.loop import RefinementLoop  # noqa: E402
from app.refinement.tasks import load_corpus, split_task_sets  # noqa: E402

CORPUS = load_corpus()
pytestmark = pytest.mark.skipif(len(CORPUS) < 20, reason="eval/corpus not present")


def _config(null_passed: bool = True) -> HarnessConfig:
    cfg = HarnessConfig(
        budget=RefinementBudget(branch_factor=3, cheap_eval_tasks=2, survivors=2,
                                holdout_tasks=4, regression_tasks=2, reps=2,
                                max_wall_clock_minutes=30),
        gate=GateSettings(min_tasks=4, min_reps=2, max_cost=0.25, drift_retest_every=1),
    )
    cfg.set_noise_floor(NoiseFloor(
        agent="synthetic", model_version="synthetic-oracle-v1",
        metrics={"tool_calls": 0.02, "tokens": 0.02, "redundant_reads": 0.1, "success": 0.5},
        null_test_passed=null_passed, sabotage_detected=True, n_tasks=20, reps=5,
        experiment_file="TEST FIXTURE", measured_at="n/a", command="n/a",
    ))
    return cfg


async def _world(tmp_path: Path, cfg: HarnessConfig):
    xp = ExperienceStore.sqlite(tmp_path / "xp.db")
    await xp.setup()
    state = SQLiteStateStore(tmp_path / "state.db")
    await state.setup()
    sets = split_task_sets(CORPUS, seed=7)
    runtime = EvaluationRuntime(
        state=state, xp=xp, snapshots=SnapshotStore(tmp_path / "snap"), tasks=CORPUS,
        adapter_factory=SyntheticAgent, verifier=Verifier.subprocess_for_tests(),
        work_root=tmp_path / "work", lease_ttl_s=30, poll_interval_s=0.02,
    )
    memory = await xp.create_memory_version([])
    h0 = await xp.create_harness(HarnessSnapshot(), memory_version=memory.id,
                                 status="active", label="H0")
    project = await xp.create_project(ProjectRecord(
        root=str(tmp_path / "repo"), agent="synthetic", active_harness=h0.id,
        h0_harness=h0.id, memory_version=memory.id,
    ))
    loop = RefinementLoop(
        xp=xp, runtime=runtime, tasks=CORPUS, task_sets=sets, config=cfg,
        agent="synthetic", model_version="synthetic-oracle-v1", seed=3, n_workers=2,
        n_resamples=500,
    )
    return xp, loop, project, sets


async def test_policy_holds_until_evidence_then_a_full_cycle_records_everything(tmp_path):
    xp, loop, project, sets = await _world(tmp_path, _config())

    outcome = await loop.cycle(project)
    assert outcome.status == "no_evidence"

    # A few observed tasks are not evidence (the policy is right to hold) …
    for i in range(5):
        await loop.run_task(project, sets.trigger[i % len(sets.trigger)], seed=100 + i)
    assert (await loop.cycle(project)).status == "no_evidence"
    # … twenty are (tasks_since_last_reflection >= 20). The synthetic
    # agent's habits produce full-suite reruns, redundant reads, blind retries.
    for i in range(5, 20):
        await loop.run_task(project, sets.trigger[i % len(sets.trigger)], seed=100 + i)

    outcome = await loop.cycle(project)
    assert outcome.status in {"promoted", "rejected"}, outcome.reason
    assert outcome.reflection_id is not None

    [reflection] = await xp.list_reflections(project_id=project.id)
    assert reflection.report["should_evolve"]
    patterns = await xp.list_failure_patterns()
    assert patterns and all(len(p.trajectory_ids) == len(p.divergence_steps) for p in patterns)

    mutations = await xp.list_mutations(parent_harness=project.h0_harness)
    assert 1 <= len(mutations) <= 3
    assert all(m.predicted_fix for m in mutations)
    comparisons = await xp.list_comparisons(parent_harness=project.h0_harness)
    trigger = [c for c in comparisons if c.task_set == "trigger"]
    assert len(trigger) == len(mutations)
    assert all(c.decision == "report" for c in trigger)  # never a promotion criterion

    reloaded = await xp.get_project(project_id=project.id)
    if outcome.status == "promoted":
        holdout = [c for c in comparisons if c.task_set == "holdout"]
        assert any(c.decision == "promote" for c in holdout)
        assert reloaded.active_harness == outcome.promoted
        assert (await xp.get_harness(project.h0_harness)).status == "archived"
        [event] = await xp.list_harness_events(project.id, kind="promote")
        assert event.details["evidence"]["divergence_steps"]
        # drift_retest_every=1: the chain is immediately re-tested against H0
        assert await xp.list_harness_events(project.id, kind="drift_check")
    else:
        assert reloaded.active_harness == project.h0_harness
        statuses = {(await xp.get_harness(m.child_harness)).status for m in mutations}
        assert statuses == {"rejected"}


async def test_no_promotion_without_a_trustworthy_noise_floor(tmp_path):
    xp, loop, project, sets = await _world(tmp_path, _config(null_passed=False))
    for i in range(20):
        await loop.run_task(project, sets.trigger[i % len(sets.trigger)], seed=200 + i)
    outcome = await loop.cycle(project)
    assert outcome.status in {"no_noise_floor", "no_lesson"}
    assert await xp.list_mutations() == []
    assert (await xp.get_project(project_id=project.id)).active_harness == project.h0_harness
