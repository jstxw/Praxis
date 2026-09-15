"""Regression pins for defects found by the post-build review and by the
v1 synthetic experiment results (docs/DECISIONS.md D13, D14)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agents.base import HarnessBinding  # noqa: E402
from app.agents.claude_code import fork_session, session_dir_for  # noqa: E402
from app.agents.recorder import record_run  # noqa: E402
from app.continual.experience import (  # noqa: E402
    EvaluationRecord,
    ExperienceStore,
    StepRecord,
    TrajectoryRecord,
)
from app.continual.harness_object import HarnessSnapshot, render_context  # noqa: E402
from app.continual.snapshots import SnapshotStore  # noqa: E402
from app.meta_harness.persistence import get_dsn  # noqa: E402
from app.refinement.config import GateSettings, NoiseFloor  # noqa: E402
from app.refinement.evaluation import RunResult, RunSpec  # noqa: E402
from app.refinement.gate import compare_metric, judge_candidates  # noqa: E402
from app.refinement.loop import RefinementLoop, claimed_chain_improvement  # noqa: E402
from app.refinement.tasks import TaskSets, load_corpus  # noqa: E402

CORPUS = load_corpus()


def _result(arm, task, rep, *, calls, context_tokens=0, tokens_complete=True) -> RunResult:
    t = TrajectoryRecord(branch_run_id="b", harness_id=arm, memory_version="m", task_id=task,
                         agent="claude", result="success", tool_calls=calls, tokens=calls * 100,
                         context_tokens=context_tokens,
                         metadata={"tokens_complete": tokens_complete})
    e = EvaluationRecord(trajectory_id=t.id, success=True, isolation="docker", tool_calls=calls,
                         tokens=calls * 100, redundant_reads=0, audit={"regressions": 0})
    return RunResult(spec=RunSpec(index=0, arm=arm, task_id=task, rep=rep, seed=0,
                                  method="scratch"), trajectory=t, evaluation=e)


def test_count_metrics_with_zero_parent_use_absolute_deltas():
    """v1 reported context_tokens improvement −1.37e11 (relative over a 0 mean)."""
    results = []
    for t in range(4):
        for r in range(3):
            results.append(_result("parent", f"t{t}", r, calls=10, context_tokens=0))
            results.append(_result("child", f"t{t}", r, calls=9, context_tokens=137))
    ctx = compare_metric(results, "parent", "child", "context_tokens", n_resamples=200)
    assert ctx.improvement == pytest.approx(-137.0)
    calls = compare_metric(results, "parent", "child", "tool_calls", n_resamples=200)
    assert calls.improvement == pytest.approx(0.1)  # still relative


def test_incomplete_token_counts_block_the_cost_check():
    results = []
    for t in range(10):
        for r in range(5):
            results.append(_result("parent", f"t{t}", r, calls=10 + r % 2))
            results.append(_result("child", f"t{t}", r, calls=6 + r % 2,
                                   tokens_complete=not (t == 0 and r == 0)))
    floor = NoiseFloor(agent="claude", model_version="m",
                       metrics={"tool_calls": 0.01, "tokens": 0.01, "success": 0.1},
                       null_test_passed=True, sabotage_detected=True, n_tasks=20, reps=5,
                       experiment_file="fixture", measured_at="n/a", command="n/a")
    [v] = judge_candidates(holdout=results, regression=None, parent_arm="parent",
                           child_arms=["child"], settings=GateSettings(), noise_floor=floor,
                           complexity_delta={}, n_resamples=300)
    assert any("token counts incomplete" in r for r in v.reasons)


def test_relative_chain_improvements_compound():
    assert claimed_chain_improvement([0.25, 0.25, 0.25], relative=True) == pytest.approx(0.578125)
    assert claimed_chain_improvement([0.1, None, 0.2], relative=False) == pytest.approx(0.3)


def test_fork_session_rewrites_non_ascii_workspace_paths(tmp_path):
    projects = tmp_path / "projects"
    src, dst = tmp_path / "josé" / "src", tmp_path / "josé" / "dst"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    sid = "55555555-5555-5555-5555-555555555555"
    rows = [
        {"type": "user", "uuid": "u", "sessionId": sid, "cwd": str(src), "message": {"content": "go"}},
        {"type": "assistant", "uuid": "a", "sessionId": sid, "message": {"content": [
            {"type": "tool_use", "id": "x", "name": "Read", "input": {"file_path": f"{src}/f.py"}}]}},
        {"type": "user", "uuid": "r", "sessionId": sid, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
    ]
    d = session_dir_for(src, projects)
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    out = fork_session(d / f"{sid}.jsonl", cut_after_tool_results=1, src_cwd=str(src),
                       dst_cwd=str(dst), new_session_id="66666666-6666-6666-6666-666666666666",
                       projects_dir=projects)
    text = out.read_text()
    assert str(src) not in text and str(dst) in text


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_step_output_with_nul_bytes_is_storable(backend, tmp_path, postgres_available):
    if backend == "postgres" and not postgres_available:
        pytest.skip("Postgres not reachable")
    xp = (ExperienceStore.sqlite(tmp_path / "x.db") if backend == "sqlite"
          else await ExperienceStore.postgres(get_dsn()))
    await xp.setup()
    t = TrajectoryRecord(branch_run_id="b", harness_id="h", memory_version="m", task_id="t",
                         agent="claude", result="failure")
    assert await xp.record_trajectory(t, [StepRecord(step=1, kind="tool_call", tool="read_file",
                                                     output="bin\x00ary")])
    [step] = await xp.get_steps(t.id)
    assert step.output == "binary"
    await xp.close()


class _NoRuntime:
    def adapter_factory(self):  # pragma: no cover - never reached
        raise AssertionError


@pytest.mark.skipif(len(CORPUS) < 20, reason="eval/corpus not present")
async def test_holdout_tasks_never_reach_reflection(tmp_path):
    from app.continual.experience import ProjectRecord
    from app.refinement.config import HarnessConfig

    xp = ExperienceStore.sqlite(tmp_path / "xp.db")
    await xp.setup()
    sets = TaskSets(trigger=["task-001-fix-typo"], holdout=["task-006-fix-recursion"],
                    regression=["task-011-add-chunked"], seed=0)
    loop = RefinementLoop(xp=xp, runtime=_NoRuntime(), tasks=CORPUS, task_sets=sets,
                          config=HarnessConfig(), agent="synthetic", model_version="v")
    memory = await xp.create_memory_version([])
    h0 = await xp.create_harness(HarnessSnapshot(), memory_version=memory.id, label="H0")
    project = await xp.create_project(ProjectRecord(root=str(tmp_path), agent="synthetic",
                                                    active_harness=h0.id, h0_harness=h0.id,
                                                    memory_version=memory.id))
    with pytest.raises(ValueError, match="holdout"):
        await loop.run_task(project, "task-006-fix-recursion", seed=0)
    # Even if a holdout trajectory is recorded some other way, reflection skips it.
    for task_id in ("task-006-fix-recursion", "task-001-fix-typo"):
        t = TrajectoryRecord(branch_run_id="wrap-x", harness_id=h0.id, memory_version=memory.id,
                             task_id=task_id, agent="synthetic", result="failure")
        await xp.record_trajectory(t, [StepRecord(step=0, kind="context")])
        await xp.record_evaluation(EvaluationRecord(trajectory_id=t.id, success=False,
                                                    isolation="docker"))
    views = await loop._recent_views(project)
    assert [v.trajectory.task_id for v in views] == ["task-001-fix-typo"]


@pytest.mark.skipif(len(CORPUS) < 20, reason="eval/corpus not present")
def test_terminated_runs_mark_tokens_incomplete_for_end_reporting_adapters(tmp_path):
    from app.agents.base import AgentEvent
    from app.continual.harness_object import Control

    class EndReporter:
        name = "claude"
        supports_fork = False
        reports_usage_at_end = True

        async def start_task(self, *a, **k):
            return "s"

        async def stream_events(self):
            for i in range(10):
                yield AgentEvent(kind="tool_call", tool="read_file", input={"path": "x"})
            yield AgentEvent(kind="usage", tokens=5000)  # never reached: cap hits first

        async def inject_context(self, c):
            pass

        async def terminate(self):
            pass

    task = CORPUS["task-001-fix-typo"]
    snap = HarnessSnapshot(control=Control(max_tool_calls=3))
    binding = HarnessBinding(harness_id="h", memory_version="m", snapshot=snap, memory=[],
                             context=render_context(snap, [], task_text=task.instruction))
    staged = asyncio.run(record_run(EndReporter(), task, binding, tmp_path / "ws",
                                    SnapshotStore(tmp_path / "s"), seed=0))
    assert staged.status == "timeout"
    assert staged.metadata["tokens_complete"] is False
