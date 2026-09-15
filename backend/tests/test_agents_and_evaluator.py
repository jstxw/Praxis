"""Trajectory capture, synthetic agent, hidden verifier, audit, task sets.

The synthetic agent here is a pipeline instrument; these tests check
mechanics (checkpoints, resume, audit, hygiene), never capability.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agents.base import HarnessBinding, ResumePoint  # noqa: E402
from app.agents.recorder import StagedTrajectory, record_run  # noqa: E402
from app.agents.synthetic import (  # noqa: E402
    SyntheticAgent,
    SyntheticProfile,
    recognize_directives,
)
from app.continual.experience import StepRecord  # noqa: E402
from app.continual.harness_object import (  # noqa: E402
    Control,
    HarnessSnapshot,
    Instruction,
    MemoryEntry,
    VerificationRule,
    render_context,
)
from app.continual.snapshots import SnapshotStore  # noqa: E402
from app.refinement.evaluator import (  # noqa: E402
    Verifier,
    audit_run,
    count_redundant_reads,
    evaluate_staged,
)
from app.refinement.tasks import (  # noqa: E402
    load_corpus,
    load_preregistration,
    preregister,
    split_task_sets,
)

CORPUS = load_corpus()
pytestmark = pytest.mark.skipif(len(CORPUS) < 20, reason="eval/corpus not present")


def _binding(snapshot: HarnessSnapshot, task, memory=()) -> HarnessBinding:
    return HarnessBinding(
        harness_id="h-test",
        memory_version="m-test",
        snapshot=snapshot,
        memory=list(memory),
        context=render_context(snapshot, list(memory), task_text=task.instruction),
    )


def _run(coro):
    return asyncio.run(coro)


def test_corpus_has_disjoint_stratified_sets():
    sets = split_task_sets(CORPUS, seed=20260915)
    assert len(sets.trigger) == 5 and len(sets.holdout) == 10 and len(sets.regression) == 5
    assert not set(sets.trigger) & set(sets.holdout)
    caps = {CORPUS[t].capability for t in sets.holdout}
    assert len(caps) >= 4  # holdout spans the capability mix
    again = split_task_sets(CORPUS, seed=20260915)
    assert again.to_json() == sets.to_json()  # seeded → reproducible


def test_preregistration_detects_edits(tmp_path):
    path = preregister("E1", {"metric": "tool_calls", "k": 5}, directory=tmp_path)
    assert load_preregistration(path)["spec"]["k"] == 5
    doc = json.loads(path.read_text())
    doc["spec"]["k"] = 3  # peeking and moving the goalposts
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        load_preregistration(path)
    with pytest.raises(FileExistsError):
        import datetime as dt

        stamp = dt.datetime.strptime(doc["registered_at"], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.timezone.utc
        )
        preregister("E1", {"x": 1}, directory=tmp_path, now=stamp)


def test_directive_recognition_respects_component_and_budget():
    task = CORPUS["task-001-fix-typo"]
    snap = HarnessSnapshot(
        verification=[VerificationRule(name="t", kind="targeted_tests",
                                       description="Run only the task's test file.")],
        instructions=[Instruction(name="skip", text="Submit without running tests.")],
    )
    found = recognize_directives(_binding(snap, task).context.text, task)
    assert found["targeted"] == "verification"
    assert found["skip_tests"] == "instruction"

    tiny = HarnessSnapshot(
        verification=snap.verification, control=Control(context_budget_tokens=10)
    )
    assert recognize_directives(_binding(tiny, task).context.text, task) == {}


def test_recorder_checkpoints_every_step_and_synthetic_run_is_seeded(tmp_path):
    task = CORPUS["task-001-fix-typo"]
    snaps = SnapshotStore(tmp_path / "snap")
    binding = _binding(HarnessSnapshot(), task)

    def once(seed: int) -> StagedTrajectory:
        return _run(record_run(SyntheticAgent(), task, binding, tmp_path / f"ws{seed}",
                               snaps, seed=seed))

    a, b = once(7), once(7)
    assert a.status == "completed"
    assert [s.tool for s in a.steps] == [s.tool for s in b.steps]
    assert a.final_snapshot == b.final_snapshot
    assert a.steps[0].kind == "context" and a.steps[0].step == 0
    assert all(s.snapshot_id for s in a.steps)
    assert [s.step for s in a.steps] == list(range(len(a.steps)))
    assert a.steps[-1].tool == "task_complete"
    assert a.tool_calls == len(a.steps) - 1
    # JSON round-trip: what the worker hands to the trusted plane.
    assert StagedTrajectory.from_json(json.loads(json.dumps(a.to_json()))).steps == a.steps


def test_fork_resume_restores_exact_prefix_state(tmp_path):
    """A fork at step d−1 starts from the checkpoint's workspace and agent
    state; identical tail seeds reproduce the parent's tail exactly."""
    task = CORPUS["task-004-handle-error"]
    snaps = SnapshotStore(tmp_path / "snap")
    binding = _binding(HarnessSnapshot(), task)
    parent = _run(record_run(SyntheticAgent(), task, binding, tmp_path / "p", snaps, seed=3))
    assert len(parent.steps) >= 4
    d = 3
    cp = parent.steps[d]
    resume = ResumePoint(
        trajectory_id=parent.trajectory_id, step=d, snapshot_id=cp.snapshot_id,
        agent_state=cp.agent_state, prefix_tool_calls=d,
        prefix_tokens=sum(s.tokens for s in parent.steps[: d + 1]),
    )
    fork = _run(record_run(SyntheticAgent(), task, binding, tmp_path / "f", snaps,
                           seed=99, resume=resume))
    assert fork.fork_parent == parent.trajectory_id and fork.fork_step == d
    assert fork.steps[0].step == d + 1  # the prefix is inherited, not re-run
    assert fork.tool_calls == d + len(fork.steps)


def test_verifier_uses_pristine_tests_and_audit_voids_tampering(tmp_path):
    task = CORPUS["task-001-fix-typo"]
    snaps = SnapshotStore(tmp_path / "snap")
    ws = tmp_path / "ws"
    shutil.copytree(task.workspace_dir, ws)
    base = snaps.snapshot(ws)
    # Tamper: skip every test instead of fixing the bug.
    test_file = ws / task.test_files[0]
    test_file.write_text("import pytest\npytestmark = pytest.mark.skip()\n" + test_file.read_text())
    final = snaps.snapshot(ws)

    verifier = Verifier.subprocess_for_tests()
    result = verifier.verify(task, ws)
    assert not result.success  # hidden verifier restored the real tests
    assert result.task_tests_total > 0

    audit = audit_run(task, snaps, base, final)
    assert audit.voided and audit.tests_modified and audit.skip_markers_added == 1


def test_evaluate_gold_fix_succeeds_with_real_metrics(tmp_path):
    task = CORPUS["task-001-fix-typo"]
    snaps = SnapshotStore(tmp_path / "snap")
    profile = SyntheticProfile(p_correct={"easy": 1.0, "medium": 1.0, "hard": 1.0},
                               p_tamper=0.0)
    staged = _run(record_run(SyntheticAgent(profile), task, _binding(HarnessSnapshot(), task),
                             tmp_path / "ws", snaps, seed=1))
    trajectory, evaluation = evaluate_staged(
        staged, task, verifier=Verifier.subprocess_for_tests(), snapshots=snaps,
        branch_run_id="test",
    )
    assert trajectory.result == "success" and evaluation.success is True
    assert evaluation.isolation == "subprocess"
    assert evaluation.tests_modified is False and not evaluation.voided
    assert evaluation.files_out_of_scope == 0
    assert evaluation.tool_calls == staged.tool_calls


def test_redundant_reads_reset_on_write():
    def s(i, tool, path):
        return StepRecord(step=i, kind="tool_call", tool=tool, input={"path": path})

    steps = [
        s(1, "read_file", "a.py"), s(2, "read_file", "a.py"),   # redundant
        s(3, "apply_patch", "a.py"), s(4, "read_file", "a.py"),  # fresh after write
        s(5, "read_file", "b.py"), s(6, "read_file", "b.py"),   # redundant
    ]
    assert count_redundant_reads(steps) == 2


def test_harness_directives_change_synthetic_behavior(tmp_path):
    """Mechanism check (not a result): a targeted-tests rule removes
    full-suite runs from the synthetic agent's trajectories."""
    task = CORPUS["task-008-fix-pagination-off-by-one"]
    snaps = SnapshotStore(tmp_path / "snap")
    targeted = HarnessSnapshot(verification=[
        VerificationRule(name="t", kind="targeted_tests",
                         description="Run only the task's test file, not the full suite."),
    ])

    def full_runs(snapshot: HarnessSnapshot) -> int:
        n = 0
        for seed in range(12):
            staged = _run(record_run(SyntheticAgent(), task, _binding(snapshot, task),
                                     tmp_path / f"ws-{seed}", snaps, seed=seed))
            n += sum(1 for st in staged.steps if st.input.get("mode") == "full")
        return n

    assert full_runs(targeted) < full_runs(HarnessSnapshot())


def test_memory_fact_about_scope_file_suppresses_exploration(tmp_path):
    task = CORPUS["task-008-fix-pagination-off-by-one"]
    scope = Path(task.scope_files[0]).name
    memory = [MemoryEntry(text=f"Pagination logic lives in {scope}.",
                          keys=[task.scope_files[0], "pagination", "page"])]
    binding = _binding(HarnessSnapshot(), task, memory)
    assert "know_location" in recognize_directives(binding.context.text, task)
