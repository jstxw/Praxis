"""E0–E5 runners on a tiny fixed split (mechanics, not results).

Runs on the synthetic agent with the subprocess verifier and writes into
temporary directories; the experiment protocol itself (pre-registration,
null/sabotage tests, noise-floor provenance, fork-vs-scratch reporting)
is what is under test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agents.synthetic import MODEL_VERSION, SyntheticAgent  # noqa: E402
from app.continual.experience import ExperienceStore  # noqa: E402
from app.continual.snapshots import SnapshotStore  # noqa: E402
from app.meta_harness.sqlite_store import SQLiteStateStore  # noqa: E402
from app.refinement import experiments as ex  # noqa: E402
from app.refinement.config import HarnessConfig  # noqa: E402
from app.refinement.evaluation import EvaluationRuntime  # noqa: E402
from app.refinement.evaluator import Verifier  # noqa: E402
from app.refinement.tasks import TaskSets, load_corpus  # noqa: E402

CORPUS = load_corpus()
pytestmark = pytest.mark.skipif(len(CORPUS) < 20, reason="eval/corpus not present")
SETS = TaskSets(
    trigger=["task-001-fix-typo", "task-020-handle-safe-divide"],
    holdout=["task-006-fix-recursion", "task-011-add-chunked"],
    regression=["task-015-refactor-shipping-constants"],
    seed=1,
)


async def _ctx(tmp_path: Path, monkeypatch) -> ex.ExperimentContext:
    monkeypatch.setenv("HARNESS_PREREG_DIR", str(tmp_path / "prereg"))
    xp = ExperienceStore.sqlite(tmp_path / "xp.db")
    await xp.setup()
    state = SQLiteStateStore(tmp_path / "state.db")
    await state.setup()
    runtime = EvaluationRuntime(
        state=state, xp=xp, snapshots=SnapshotStore(tmp_path / "snap"), tasks=CORPUS,
        adapter_factory=SyntheticAgent, verifier=Verifier.subprocess_for_tests(),
        work_root=tmp_path / "work", lease_ttl_s=30, poll_interval_s=0.02,
    )
    config = HarnessConfig()
    config.gate.min_tasks = 2
    config.gate.min_reps = 2
    return ex.ExperimentContext(
        xp=xp, state=state, runtime=runtime, tasks=CORPUS, sets=SETS, config=config,
        agent="synthetic", model_version=MODEL_VERSION, command="pytest (test run)",
        n_workers=2, seed=5, n_resamples=500, results_dir=tmp_path / "results",
        config_path=tmp_path / "config.json",
    )


async def test_experiments_refuse_without_matching_preregistration(tmp_path, monkeypatch):
    ctx = await _ctx(tmp_path, monkeypatch)
    with pytest.raises(ex.PreregistrationMismatch):
        ex.ensure_preregistered("E1", ctx, register=False)
    path = ex.ensure_preregistered("E1", ctx, register=True)
    assert ex.ensure_preregistered("E1", ctx, register=False) == path
    ctx.sets = TaskSets(trigger=["a"], holdout=["b"], regression=["c"], seed=9)
    with pytest.raises(ex.PreregistrationMismatch):  # different task sets than registered
        ex.ensure_preregistered("E1", ctx, register=False)


async def test_e0_durability_under_a_worker_kill(tmp_path, monkeypatch):
    ctx = await _ctx(tmp_path, monkeypatch)
    prereg = ex.ensure_preregistered("E0", ctx, register=True)
    result = json.loads((await ex.run_e0(ctx, preregistration=prereg, dst_seeds=20)).read_text())
    assert result["passed"], result
    assert result["duplicate_records"] == 0
    assert result["killed_branch_final_fence"] >= 2
    assert result["label"].startswith("SYNTHETIC AGENT")


async def test_e1_writes_noise_floor_with_provenance_then_e4_uses_it(tmp_path, monkeypatch):
    ctx = await _ctx(tmp_path, monkeypatch)
    prereg = ex.ensure_preregistered("E1", ctx, register=True)
    # small reps for speed: the registration's reps is authoritative, so re-register
    doc = json.loads(prereg.read_text())
    assert doc["spec"]["reps"] == 5
    path = await ex.run_e1(ctx, preregistration=prereg, reps=5)
    result = json.loads(path.read_text())

    assert set(result["noise_floor"]) >= {"tool_calls", "success", "tokens"}
    assert isinstance(result["null_test"]["passed"], bool)
    assert result["sabotage_test"]["detected"], result["sabotage_test"]["metrics"]["success"]
    saved = HarnessConfig.load(tmp_path / "config.json")
    floor = saved.noise_floors[f"synthetic:{MODEL_VERSION}"]
    assert floor.experiment_file.endswith(path.name)
    assert floor.null_test_passed == result["null_test"]["passed"]

    if floor.null_test_passed:
        ctx.config = saved
        ctx.config.gate.min_tasks, ctx.config.gate.min_reps = 2, 2
        prereg4 = ex.ensure_preregistered("E4", ctx, register=True)
        e4 = json.loads((await ex.run_e4(ctx, preregistration=prereg4, reps=5)).read_text())
        assert e4["noise_floor_source"] == floor.experiment_file
        assert "promotable" in e4["verdict"]


async def test_e2_reports_variance_and_cost_ratios_for_both_methods(tmp_path, monkeypatch):
    ctx = await _ctx(tmp_path, monkeypatch)
    prereg = ex.ensure_preregistered("E2", ctx, register=True)
    result = json.loads((await ex.run_e2(ctx, preregistration=prereg, reps=5)).read_text())
    assert set(result["fork_points"]) == set(SETS.holdout)
    tc = result["metrics"]["tool_calls"]
    assert {"fork", "scratch", "variance_ratio_scratch_over_fork", "agree_sign"} <= set(tc)
    cost = result["cost"]
    assert cost["executed_tool_calls_fork_tails"] < cost["executed_tool_calls_scratch"]


async def test_e5_compares_trigger_and_holdout_for_one_mutation(tmp_path, monkeypatch):
    ctx = await _ctx(tmp_path, monkeypatch)
    prereg = ex.ensure_preregistered("E5", ctx, register=True)
    result = json.loads((await ex.run_e5(ctx, preregistration=prereg, reps=5)).read_text())
    assert {"trigger", "holdout", "kill_criterion", "mutation"} <= set(result)
    assert isinstance(result["kill_criterion"]["overfitting"], bool)


def test_cohens_kappa():
    assert ex.cohens_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0
    assert ex.cohens_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0
