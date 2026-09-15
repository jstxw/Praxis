"""Experience store contract (DESIGN §1), against SQLite (local mode)
and Postgres (service mode, gated on reachability) with identical tests."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.continual.experience import (  # noqa: E402
    ComparisonRecord,
    EvaluationRecord,
    ExperienceStore,
    FailurePatternRecord,
    HarnessEventRecord,
    MutationRecord,
    ProjectRecord,
    StepRecord,
    TrajectoryRecord,
)
from app.continual.harness_object import (  # noqa: E402
    HarnessSnapshot,
    Instruction,
    MemoryEntry,
)
from app.meta_harness.persistence import get_dsn  # noqa: E402


@pytest.fixture(params=["sqlite", "postgres"])
async def xp(request, tmp_path, postgres_available):
    if request.param == "sqlite":
        store = ExperienceStore.sqlite(tmp_path / "xp.db")
    else:
        if not postgres_available:
            pytest.skip("Postgres not reachable at configured DSN")
        store = await ExperienceStore.postgres(get_dsn())
    await store.setup()
    yield store
    await store.close()


async def _harness(xp: ExperienceStore, **kw):
    mem = await xp.create_memory_version([])
    return await xp.create_harness(HarnessSnapshot(), memory_version=mem.id, **kw), mem


async def test_memory_versions_derive_without_mutating_parent(xp):
    base = await xp.create_memory_version(
        [MemoryEntry(id="a", text="fact a"), MemoryEntry(id="b", text="fact b")]
    )
    child = await xp.derive_memory_version(
        base.id, add=[MemoryEntry(id="c", text="fact c")], remove_ids=["a"]
    )
    assert child.parent_id == base.id
    assert child.entry_ids() == {"b", "c"}
    reloaded = await xp.get_memory_version(base.id)
    assert reloaded.entry_ids() == {"a", "b"}
    assert reloaded.size_bytes > 0


async def test_harness_content_is_immutable_only_status_moves(xp):
    mem = await xp.create_memory_version([])
    snap = HarnessSnapshot(instructions=[Instruction(name="i", text="be brief")])
    h = await xp.create_harness(snap, memory_version=mem.id, label="H0")
    snap.instructions.append(Instruction(name="later", text="mutated after create"))
    stored = await xp.get_harness(h.id)
    assert len(stored.snapshot.instructions) == 1  # caller mutation did not leak
    assert stored.digest == h.digest
    assert stored.complexity["instruction_count"] == 1

    await xp.set_harness_status(h.id, "active")
    assert (await xp.get_harness(h.id)).status == "active"
    with pytest.raises(ValueError):
        await xp.set_harness_status(h.id, "promoted")
    child = await xp.create_harness(HarnessSnapshot(), memory_version=mem.id, parent_id=h.id)
    assert [r.id for r in await xp.lineage(child.id)] == [child.id, h.id]


async def test_trajectory_insert_is_atomic_and_idempotent(xp):
    h, mem = await _harness(xp)
    t = TrajectoryRecord(
        branch_run_id=f"br-{uuid.uuid4().hex[:6]}", harness_id=h.id,
        memory_version=mem.id, task_id="task-001", agent="synthetic",
        result="success", tool_calls=3, tokens=1200,
    )
    steps = [StepRecord(step=i, kind="tool_call", tool="read_file",
                        input={"path": "a.py"}, snapshot_id=f"s{i}") for i in range(1, 4)]
    assert await xp.record_trajectory(t, steps)
    assert not await xp.record_trajectory(t, steps)  # duplicate id is a no-op
    got = await xp.get_trajectory(t.id)
    assert got.memory_version == mem.id and got.agent == "synthetic"
    assert [s.step for s in await xp.get_steps(t.id)] == [1, 2, 3]
    assert [s.step for s in await xp.get_steps(t.id, upto=2)] == [1, 2]

    with pytest.raises(ValueError):
        await xp.record_trajectory(
            TrajectoryRecord(branch_run_id="x", harness_id=h.id, memory_version=mem.id,
                             task_id="t", agent="synthetic", result="meh"),
            [],
        )


async def test_fork_trajectory_full_steps_include_parent_prefix(xp):
    h, mem = await _harness(xp)
    parent = TrajectoryRecord(branch_run_id="p", harness_id=h.id, memory_version=mem.id,
                              task_id="t", agent="synthetic", result="failure")
    await xp.record_trajectory(
        parent, [StepRecord(step=i, kind="tool_call", tool=f"t{i}") for i in range(1, 6)]
    )
    fork = TrajectoryRecord(branch_run_id="f", harness_id=h.id, memory_version=mem.id,
                            task_id="t", agent="synthetic", result="success",
                            fork_parent=parent.id, fork_step=3)
    await xp.record_trajectory(
        fork, [StepRecord(step=4, kind="tool_call", tool="tail4")]
    )
    tools = [s.tool for s in await xp.full_steps(fork.id)]
    assert tools == ["t1", "t2", "t3", "tail4"]


async def test_evaluation_once_per_trajectory_and_join(xp):
    h, mem = await _harness(xp)
    t = TrajectoryRecord(branch_run_id="b", harness_id=h.id, memory_version=mem.id,
                         task_id="t", agent="synthetic", result="success")
    await xp.record_trajectory(t, [])
    ev = EvaluationRecord(trajectory_id=t.id, success=True, isolation="docker",
                          tests_modified=False, audit={"reasons": []})
    assert await xp.record_evaluation(ev)
    assert not await xp.record_evaluation(
        EvaluationRecord(trajectory_id=t.id, success=False, isolation="docker")
    )
    got = await xp.get_evaluation(t.id)
    assert got.success is True and got.tests_modified is False and got.voided is False
    joined = await xp.evaluated_trajectories(harness_id=h.id)
    assert [(tr.id, e.success) for tr, e in joined] == [(t.id, True)]


async def test_patterns_mutations_comparisons_projects_events(xp):
    h0, mem = await _harness(xp, label="H0")
    h1 = await xp.create_harness(HarnessSnapshot(), memory_version=mem.id, parent_id=h0.id)

    with pytest.raises(ValueError):
        await xp.record_failure_pattern(FailurePatternRecord(
            name="bad", description="", trajectory_ids=["a", "b"], divergence_steps=[1],
            affected_component="skill", scope="local",
        ))
    pattern = FailurePatternRecord(
        name="redundant_full_suite", description="ran full suite repeatedly",
        trajectory_ids=["t1", "t2", "t3"], divergence_steps=[4, 7, 5],
        affected_component="verification", scope="local",
    )
    await xp.record_failure_pattern(pattern)
    got = await xp.get_failure_pattern(pattern.id)
    assert got.divergence_steps == [4, 7, 5] and got.occurrence_count == 3

    mutation = MutationRecord(
        parent_harness=h0.id, child_harness=h1.id, pattern_id=pattern.id,
        hypothesis="targeted tests cut tool calls", predicted_fix=["task-002"],
        predicted_risk=["task-009"], payload={"type": "verification"},
    )
    await xp.record_mutation(mutation)
    assert (await xp.get_mutation_for_child(h1.id)).predicted_fix == ["task-002"]

    comparison = ComparisonRecord(
        parent_harness=h0.id, child_harness=h1.id, task_set="holdout", method="fork",
        n_tasks=10, n_reps=5, metric="tool_calls", delta=-0.2, ci_lower=-0.3,
        ci_upper=-0.1, p_value=0.01, decision="promote", details={"reasons": []},
    )
    await xp.record_comparison(comparison)
    assert (await xp.list_comparisons(child_harness=h1.id))[0].decision == "promote"

    root = f"/tmp/project-{uuid.uuid4().hex[:8]}"
    project = await xp.create_project(ProjectRecord(
        root=root, agent="claude", active_harness=h0.id, h0_harness=h0.id,
        memory_version=mem.id,
    ))
    await xp.update_project(project.id, active_harness=h1.id, config={"k": 1})
    reloaded = await xp.get_project(root=root)
    assert reloaded.active_harness == h1.id and reloaded.config == {"k": 1}

    await xp.record_harness_event(HarnessEventRecord(
        project_id=project.id, kind="promote", harness_id=h1.id, from_harness=h0.id,
        summary="H0 → H1",
    ))
    events = await xp.list_harness_events(project.id, kind="promote")
    assert [e.summary for e in events] == ["H0 → H1"]
