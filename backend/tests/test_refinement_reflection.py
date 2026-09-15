"""Refinement policy, reflection with step attribution, mutation proposal."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agents.base import TaskSpec  # noqa: E402
from app.continual.experience import (  # noqa: E402
    EvaluationRecord,
    ExperienceStore,
    StepRecord,
    TrajectoryRecord,
)
from app.continual.harness_object import HarnessSnapshot, Skill  # noqa: E402
from app.refinement.mutation import (  # noqa: E402
    apply_to_snapshot,
    propose,
    propose_deletions,
)
from app.refinement.policy import (  # noqa: E402
    HarnessStats,
    compute_stats,
    is_observed_work,
    should_reflect,
)
from app.refinement.reflection import (  # noqa: E402
    HeuristicReflector,
    LLMReflector,
    TrajectoryView,
    detect_blind_retry,
    detect_exploration_waste,
    detect_redundant_full_suite,
    detect_unverified_submit,
)

TASK = TaskSpec(
    id="task-x", instruction="Fix pager.py so tests/test_pager.py passes",
    test_command="pytest tests/test_pager.py -q", full_test_command="pytest -q",
    scope_files=("pager.py",), test_files=("tests/test_pager.py",),
    capability="bug-fix", difficulty="medium", source_dir=Path("/nonexistent"),
)


def _s(step, tool, **inp) -> StepRecord:
    is_error = inp.pop("is_error", False)
    return StepRecord(step=step, kind="tool_call", tool=tool, input=inp, is_error=is_error)


WASTEFUL = [
    StepRecord(step=0, kind="context"),
    _s(1, "read_file", path="util.py"),                      # exploration waste
    _s(2, "read_file", path="pager.py"),
    _s(3, "apply_patch", path="pager.py"),
    _s(4, "run_bash", command="pytest -q", is_error=True),   # full suite
    _s(5, "apply_patch", path="pager.py"),                   # blind retry
    _s(6, "task_complete"),                                  # unverified submit
]


def test_detectors_report_the_first_divergence_step():
    assert detect_exploration_waste(WASTEFUL, TASK) == 1
    assert detect_redundant_full_suite(WASTEFUL, TASK) == 4
    assert detect_blind_retry(WASTEFUL, TASK) == 5
    assert detect_unverified_submit(WASTEFUL, TASK) == 6
    clean = [
        _s(1, "read_file", path="pager.py"), _s(2, "apply_patch", path="pager.py"),
        _s(3, "run_bash", command="pytest tests/test_pager.py -q"), _s(4, "task_complete"),
    ]
    for detector in (detect_exploration_waste, detect_redundant_full_suite,
                     detect_blind_retry, detect_unverified_submit):
        assert detector(clean, TASK) is None


def _view(i: int, steps, *, success: bool, task_id: str = "task-x", result=None) -> TrajectoryView:
    t = TrajectoryRecord(branch_run_id="hx-work-1", harness_id="h", memory_version="m",
                         task_id=task_id, agent="synthetic",
                         result=result or ("success" if success else "failure"), id=f"t{i}")
    e = EvaluationRecord(trajectory_id=t.id, success=success, isolation="docker",
                         audit={"regressions": 0}, files_out_of_scope=0)
    return TrajectoryView(trajectory=t, evaluation=e, steps=steps, task=TASK)


def test_heuristic_reflection_classifies_before_proposing():
    tasks = ["task-a", "task-b", "task-c"]
    views = [_view(i, WASTEFUL, success=False, task_id=tasks[i % 3]) for i in range(4)]
    report = HeuristicReflector().reflect(views)
    by_name = {p.name: p for p in report.failure_patterns}
    assert report.should_evolve
    full = by_name["redundant_full_suite"]
    assert full.classification == "harness_deficiency"
    assert full.divergence_steps == [4, 4, 4, 4]
    assert full.fork_points()[0] == ("t0", 3)  # fork at d−1
    assert by_name["exploration_waste"].scope == "global"

    # Rare → model randomness; one task only → task ambiguity; infra → environmental.
    rare = HeuristicReflector().reflect([_view(0, WASTEFUL, success=False)])
    assert all(p.classification == "model_randomness" for p in rare.failure_patterns)
    assert not rare.should_evolve
    one_task = HeuristicReflector().reflect(
        [_view(i, WASTEFUL, success=False) for i in range(3)]
    )
    assert {p.name: p.classification for p in one_task.failure_patterns}["blind_retry"] == (
        "task_ambiguity"
    )
    infra = HeuristicReflector().reflect(
        [_view(i, WASTEFUL, success=False, result="infra_error") for i in range(4)]
    )
    assert not infra.lessons()


def test_llm_reflector_drops_citations_that_do_not_exist():
    views = [_view(i, WASTEFUL, success=False, task_id=f"k{i}") for i in range(4)]
    fake = json.dumps({
        "strengths": ["reads tests"],
        "should_evolve": True,
        "failure_patterns": [
            {"name": "full_suite", "description": "d", "trajectory_ids": ["t0", "t1", "t2", "t3"],
             "divergence_steps": [4, 4, 4, 99], "affected_component": "verification",
             "scope": "local", "classification": "harness_deficiency"},
            {"name": "invented", "description": "d", "trajectory_ids": ["nope", "t1"],
             "divergence_steps": [1, 2], "affected_component": "skill", "scope": "local"},
        ],
    })
    report = LLMReflector(lambda prompt: fake).reflect(views)
    assert [p.name for p in report.failure_patterns] == ["full_suite"]
    assert report.failure_patterns[0].trajectory_ids == ["t0", "t1", "t2"]  # step 99 dropped
    assert any("invented" in d for d in report.discarded)


def test_policy_triggers_and_never_defaults_noise_floor():
    quiet = HarnessStats(5, 1, None, success_rate_drop=0.5, tool_cost_increase=0.1,
                         recent_n=5, baseline_n=10)
    assert not should_reflect(quiet, noise_floor_success=None).reflect  # floor unmeasured
    assert should_reflect(quiet, noise_floor_success=0.1).reflect
    assert should_reflect(HarnessStats(20, 0, None, 0, 0, 10, 10), noise_floor_success=None).reflect
    assert should_reflect(HarnessStats(3, 3, "blind_retry", 0, 0, 3, 0),
                          noise_floor_success=None).reflect
    assert should_reflect(HarnessStats(3, 0, None, 0, 0.5, 3, 5), noise_floor_success=None).reflect


def test_observed_work_excludes_experiments():
    def t(run, fork=None):
        return TrajectoryRecord(branch_run_id=run, harness_id="h", memory_version="m",
                                task_id="x", agent="a", result="success", fork_parent=fork)

    assert is_observed_work(t("hx-work-abc"))
    assert is_observed_work(t("wrap-session-1"))
    assert not is_observed_work(t("hx-e1-abc:3"))
    assert not is_observed_work(t("hx-work-abc", fork="p"))


async def test_compute_stats_counts_repeated_failure_signatures(tmp_path):
    xp = ExperienceStore.sqlite(tmp_path / "xp.db")
    await xp.setup()
    for i in range(4):
        tr = TrajectoryRecord(branch_run_id=f"hx-work-{i}", harness_id="h", memory_version="m",
                              task_id="task-x", agent="synthetic", result="failure")
        await xp.record_trajectory(tr, WASTEFUL)
        await xp.record_evaluation(EvaluationRecord(trajectory_id=tr.id, success=False,
                                                    isolation="docker", tool_calls=6))
    stats = await compute_stats(xp, harness_id="h", agent="synthetic", last_reflection_at=None,
                                tasks={"task-x": TASK})
    assert stats.tasks_since_last_reflection == 4
    assert stats.repeated_failure_count == 4
    assert should_reflect(stats, noise_floor_success=None).reflect


def test_mutations_attack_one_pattern_through_different_components():
    views = [_view(i, WASTEFUL, success=False, task_id=f"k{i % 3}") for i in range(4)]
    lessons = HeuristicReflector().reflect(views).lessons()
    top = next(p for p in lessons if p.name == "redundant_full_suite")
    proposals = propose([top], HarnessSnapshot(), risk_pool=["k0", "r1", "r2"])
    components = [p.component for _, p in proposals]
    assert components == ["verification", "skill", "instruction"]  # priority order
    for _, p in proposals:
        assert p.predicted_fix == top.task_ids
        assert "k0" not in p.predicted_risk and p.predicted_risk == ["r1", "r2"]

    child = apply_to_snapshot(HarnessSnapshot(), proposals[0][1])
    assert child.rule_kinds() == {"targeted_tests"}
    # Proposing again against the mutated harness skips what's already there.
    again = propose([top], child, risk_pool=[])
    assert all(p.component != "verification" for _, p in again)


def test_deletion_proposals_fight_harness_rot():
    parent = HarnessSnapshot(skills=[Skill(name="dead", description="never fires",
                                           triggers=["zzz"])])
    [delete] = propose_deletions(parent, injected_counts={}, n_recent=12)
    assert delete.op == "delete" and delete.remove_names == ["dead"]
    assert apply_to_snapshot(parent, delete).skills == []
    assert propose_deletions(parent, injected_counts={}, n_recent=3) == []
