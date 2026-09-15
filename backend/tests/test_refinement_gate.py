"""Promotion gate (DESIGN §6) on constructed paired results, and the
noise-floor config it depends on."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.continual.experience import EvaluationRecord, TrajectoryRecord  # noqa: E402
from app.refinement.config import (  # noqa: E402
    GateSettings,
    HarnessConfig,
    NoiseFloor,
    NoiseFloorUnmeasured,
)
from app.refinement.evaluation import RunResult, RunSpec  # noqa: E402
from app.refinement.gate import (  # noqa: E402
    compare_metric,
    judge_candidates,
    select_promotion,
)


def _floor(**metrics) -> NoiseFloor:
    base = {"tool_calls": 0.05, "tokens": 0.05, "redundant_reads": 0.2, "success": 0.1}
    base.update(metrics)
    return NoiseFloor(
        agent="synthetic", model_version="v1", metrics=base, null_test_passed=True,
        sabotage_detected=True, n_tasks=20, reps=5, experiment_file="x.json",
        measured_at="now", command="harness experiment e1",
    )


def _result(arm, task, rep, *, tool_calls, success=True, tokens=None, voided=False,
            redundant_reads=0) -> RunResult:
    spec = RunSpec(index=0, arm=arm, task_id=task, rep=rep, seed=rep, method="scratch")
    tokens = tokens if tokens is not None else tool_calls * 100
    trajectory = TrajectoryRecord(
        branch_run_id="b", harness_id=arm, memory_version="m", task_id=task,
        agent="synthetic", result="success" if success else "failure",
        tool_calls=tool_calls, tokens=tokens,
    )
    evaluation = EvaluationRecord(
        trajectory_id=trajectory.id, success=success, isolation="docker",
        tool_calls=tool_calls, tokens=tokens, voided=voided, tests_modified=voided,
        redundant_reads=redundant_reads, diff_size=4, files_out_of_scope=0,
        audit={"regressions": 0},
    )
    return RunResult(spec=spec, trajectory=trajectory, evaluation=evaluation)


def _arms(n_tasks=12, reps=5, *, parent_calls=10, child_calls=7, child_success=True,
          jitter=True):
    results = []
    for t in range(n_tasks):
        for r in range(reps):
            j = ((t * 7 + r * 3) % 3) - 1 if jitter else 0
            results.append(_result("parent", f"t{t}", r, tool_calls=parent_calls + j))
            results.append(_result("child", f"t{t}", r, tool_calls=child_calls + j,
                                   success=child_success))
    return results


def test_metrics_are_reported_as_improvements():
    results = _arms()
    m = compare_metric(results, "parent", "child", "tool_calls", n_resamples=2000)
    assert m.improvement == pytest.approx(0.3, abs=0.03)  # 30% fewer calls
    assert 0 < m.ci_lower <= m.improvement <= m.ci_upper
    assert m.parent_mean > m.child_mean


def test_clear_efficiency_win_with_success_held_is_promoted():
    verdicts = judge_candidates(
        holdout=_arms(), regression=_arms(n_tasks=5), parent_arm="parent",
        child_arms=["child"], settings=GateSettings(), noise_floor=_floor(),
        complexity_delta={"child": 1.0}, n_resamples=2000,
    )
    [v] = verdicts
    assert v.promotable, v.reasons
    assert select_promotion(verdicts) is v


def test_gate_rejects_each_violation_with_a_reason():
    settings = GateSettings()
    # too few holdout tasks / reps
    [v] = judge_candidates(holdout=_arms(n_tasks=5, reps=3), regression=None,
                           parent_arm="parent", child_arms=["child"], settings=settings,
                           noise_floor=_floor(), complexity_delta={}, n_resamples=500)
    assert not v.promotable
    assert any("MIN_TASKS" in r for r in v.reasons) and any("MIN_REPS" in r for r in v.reasons)

    # success collapses: efficient but wrong
    [v] = judge_candidates(holdout=_arms(child_success=False), regression=None,
                           parent_arm="parent", child_arms=["child"], settings=settings,
                           noise_floor=_floor(), complexity_delta={}, n_resamples=500)
    assert any("success guard" in r for r in v.reasons)

    # effect inside the noise floor
    [v] = judge_candidates(holdout=_arms(child_calls=10), regression=None,
                           parent_arm="parent", child_arms=["child"], settings=settings,
                           noise_floor=_floor(), complexity_delta={}, n_resamples=500)
    assert any(r.startswith("effect") for r in v.reasons)

    # audit: a voided child run blocks promotion regardless of efficiency
    results = _arms()
    results.append(_result("child", "t0", 99, tool_calls=1, voided=True))
    [v] = judge_candidates(holdout=results, regression=None, parent_arm="parent",
                           child_arms=["child"], settings=settings, noise_floor=_floor(),
                           complexity_delta={}, n_resamples=500)
    assert any(r.startswith("audit") for r in v.reasons)

    # cost: fewer calls but 50% more tokens
    results = []
    for t in range(12):
        for r in range(5):
            results.append(_result("parent", f"t{t}", r, tool_calls=10 + r % 2, tokens=1000))
            results.append(_result("child", f"t{t}", r, tool_calls=7 + r % 2, tokens=1500))
    [v] = judge_candidates(holdout=results, regression=None, parent_arm="parent",
                           child_arms=["child"], settings=settings, noise_floor=_floor(),
                           complexity_delta={}, n_resamples=500)
    assert any(r.startswith("cost") for r in v.reasons)


def test_regression_set_degradation_blocks_promotion():
    regression = []
    for t in range(5):
        for r in range(5):
            regression.append(_result("parent", f"r{t}", r, tool_calls=10))
            regression.append(_result("child", f"r{t}", r, tool_calls=7, success=r < 2))
    [v] = judge_candidates(holdout=_arms(), regression=regression, parent_arm="parent",
                           child_arms=["child"], settings=GateSettings(),
                           noise_floor=_floor(), complexity_delta={}, n_resamples=500)
    assert any(r.startswith("regression set") for r in v.reasons)


def test_complexity_penalty_prefers_the_smaller_harness():
    results = _arms()
    for t in range(12):
        for r in range(5):
            j = ((t * 7 + r * 3) % 3) - 1
            results.append(_result("big", f"t{t}", r, tool_calls=7 + j))
    verdicts = judge_candidates(
        holdout=results, regression=None, parent_arm="parent", child_arms=["child", "big"],
        settings=GateSettings(lambda_complexity=0.01), noise_floor=_floor(),
        complexity_delta={"child": 1.0, "big": 12.0}, n_resamples=2000,
    )
    assert all(v.promotable for v in verdicts), [v.reasons for v in verdicts]
    assert select_promotion(verdicts).arm == "child"


def test_noise_floor_is_never_defaulted(tmp_path):
    cfg = HarnessConfig()
    with pytest.raises(NoiseFloorUnmeasured):
        cfg.noise_floor("synthetic", "v1")
    failed = _floor()
    failed.null_test_passed = False
    cfg.set_noise_floor(failed)
    with pytest.raises(NoiseFloorUnmeasured):
        cfg.noise_floor("synthetic", "v1")  # a failed null test is not a floor

    cfg.set_noise_floor(_floor())
    path = cfg.save(tmp_path / "config.json")
    loaded = HarnessConfig.load(path)
    assert loaded.noise_floor("synthetic", "v1").floor("tool_calls") == 0.05
    with pytest.raises(NoiseFloorUnmeasured):
        loaded.noise_floor("claude", "claude-haiku-4-5")  # floors don't transfer
