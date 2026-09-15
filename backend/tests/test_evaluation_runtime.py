"""Candidate evaluation on the durable runtime.

Named after the invariants they exercise: evaluation branches go through
the same fenced lifecycle as every other branch (I1, I5), and memory is
frozen during any comparison (ARCHITECTURE §2).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agents.synthetic import SyntheticAgent  # noqa: E402
from app.continual.experience import ExperienceStore  # noqa: E402
from app.continual.harness_object import (  # noqa: E402
    HarnessSnapshot,
    MemoryEntry,
    VerificationRule,
)
from app.continual.snapshots import SnapshotStore  # noqa: E402
from app.meta_harness.persistence import get_dsn  # noqa: E402
from app.meta_harness.sqlite_store import SQLiteStateStore  # noqa: E402
from app.meta_harness.store import PostgresStateStore, StaleFenceError  # noqa: E402
from app.refinement.evaluation import (  # noqa: E402
    Arm,
    EvaluationRuntime,
    MemoryNotFrozen,
    new_run_id,
    plan_comparison,
)
from app.refinement.evaluator import Verifier  # noqa: E402
from app.refinement.tasks import load_corpus  # noqa: E402

CORPUS = load_corpus()
TASKS = ["task-001-fix-typo", "task-020-handle-safe-divide"]
pytestmark = pytest.mark.skipif(
    any(t not in CORPUS for t in TASKS), reason="eval/corpus not present"
)
TARGETED = HarnessSnapshot(verification=[
    VerificationRule(name="targeted", kind="targeted_tests",
                     description="Run only the task's test file, not the full suite."),
])


async def _setup(tmp_path: Path, state):
    xp = ExperienceStore.sqlite(tmp_path / "xp.db")
    await xp.setup()
    await state.setup()
    memory = await xp.create_memory_version([])
    h0 = await xp.create_harness(HarnessSnapshot(), memory_version=memory.id, label="H0")
    h1 = await xp.create_harness(TARGETED, memory_version=memory.id, parent_id=h0.id)
    runtime = EvaluationRuntime(
        state=state,
        xp=xp,
        snapshots=SnapshotStore(tmp_path / "snap"),
        tasks={t: CORPUS[t] for t in TASKS},
        adapter_factory=SyntheticAgent,
        verifier=Verifier.subprocess_for_tests(),
        work_root=tmp_path / "work",
        lease_ttl_s=5.0,
        poll_interval_s=0.05,
    )
    arms = [Arm("parent", h0.id, memory.id), Arm("child", h1.id, memory.id)]
    return xp, runtime, arms, memory


@pytest.fixture(params=["sqlite", "postgres"])
async def state_store(request, tmp_path, postgres_available):
    if request.param == "sqlite":
        yield SQLiteStateStore(tmp_path / "state.db")
        return
    if not postgres_available:
        pytest.skip("Postgres not reachable at configured DSN")
    store = await PostgresStateStore.connect(get_dsn())
    yield store
    await store._conn.execute("DELETE FROM branch_runs WHERE run_id LIKE 'hx-test-%%';")
    await store._conn.execute("DELETE FROM iteration_log WHERE run_id LIKE 'hx-test-%%';")
    await store.close()


async def test_i1_scratch_plan_runs_every_spec_exactly_once_interleaved(tmp_path, state_store):
    xp, runtime, arms, memory = await _setup(tmp_path, state_store)
    plan = plan_comparison(
        run_id=new_run_id(f"test-{uuid.uuid4().hex[:6]}"), arms=arms, task_ids=TASKS,
        reps=2, method="scratch", pinned_memory=memory.id, seed=11,
    )
    results = await runtime.run(plan, n_workers=2)

    keys = [(r.spec.arm, r.spec.task_id, r.spec.rep) for r in results]
    assert len(keys) == len(plan.specs) == 8
    assert len(set(keys)) == 8
    # Interleaved: each (task, rep) block holds both arms back to back.
    for i in range(0, len(plan.specs), 2):
        block = plan.specs[i:i + 2]
        assert {s.arm for s in block} == {"parent", "child"}
        assert len({(s.task_id, s.rep) for s in block}) == 1
    # Seeds are independent across arms (no shared tail randomness).
    assert plan.specs[0].seed != plan.specs[1].seed
    assert all(r.trajectory.memory_version == memory.id for r in results)
    assert all(r.evaluation.isolation == "subprocess" for r in results)
    assert await runtime.submit(plan) == 0  # resubmission is a no-op

    again = await runtime.collect(plan)
    assert [r.trajectory.id for r in again] == [r.trajectory.id for r in results]


async def test_memory_frozen_during_comparison(tmp_path):
    xp, runtime, arms, memory = await _setup(tmp_path, SQLiteStateStore(tmp_path / "s.db"))
    unrelated = await xp.create_memory_version([MemoryEntry(text="accumulated later")])
    bad = [arms[0], Arm("child", arms[1].harness_id, unrelated.id)]
    plan = plan_comparison(
        run_id=new_run_id("freeze"), arms=bad, task_ids=TASKS[:1], reps=1,
        method="scratch", pinned_memory=memory.id, seed=1,
    )
    with pytest.raises(MemoryNotFrozen):
        await runtime.submit(plan)

    # A memory *mutation* of the pinned version is a legitimate candidate.
    mutated = await xp.derive_memory_version(memory.id, add=[MemoryEntry(text="fact")])
    ok = plan_comparison(
        run_id=new_run_id("freeze-ok"), arms=[arms[0], Arm("child", arms[1].harness_id, mutated.id)],
        task_ids=TASKS[:1], reps=1, method="scratch", pinned_memory=memory.id, seed=1,
    )
    assert await runtime.submit(ok) == 2


async def test_i4_fork_plan_shares_the_prefix_and_runs_only_the_tail(tmp_path):
    xp, runtime, arms, memory = await _setup(tmp_path, SQLiteStateStore(tmp_path / "s.db"))
    source_plan = plan_comparison(
        run_id=new_run_id("source"), arms=arms[:1], task_ids=TASKS[:1], reps=1,
        method="scratch", pinned_memory=memory.id, seed=5,
    )
    [source] = await runtime.run(source_plan)
    source_steps = await xp.full_steps(source.trajectory.id)
    d = 2
    assert len(source_steps) > d + 1

    fork_plan = plan_comparison(
        run_id=new_run_id("fork"), arms=arms, task_ids=TASKS[:1], reps=2,
        method="fork", pinned_memory=memory.id, seed=6,
        fork_points={TASKS[0]: (source.trajectory.id, d)},
    )
    results = await runtime.run(fork_plan)
    assert len(results) == 4
    prefix = [(s.step, s.tool, s.snapshot_id) for s in source_steps[: d + 1]]
    for r in results:
        assert r.trajectory.fork_parent == source.trajectory.id
        assert r.trajectory.fork_step == d
        own = await xp.get_steps(r.trajectory.id)
        assert own and own[0].step == d + 1  # tail only
        full = await xp.full_steps(r.trajectory.id)
        assert [(s.step, s.tool, s.snapshot_id) for s in full[: d + 1]] == prefix
        assert r.trajectory.tool_calls >= d
    # The parent (source) trajectory was never mutated by its forks.
    assert [(s.step, s.tool) for s in await xp.full_steps(source.trajectory.id)] == [
        (s.step, s.tool) for s in source_steps
    ]


async def test_i5_reclaimed_evaluation_branch_is_recorded_once(tmp_path):
    state = SQLiteStateStore(tmp_path / "s.db")
    xp, runtime, arms, memory = await _setup(tmp_path, state)
    plan = plan_comparison(
        run_id=new_run_id("reclaim"), arms=arms[:1], task_ids=TASKS[:1], reps=1,
        method="scratch", pinned_memory=memory.id, seed=3,
    )
    await runtime.submit(plan)
    # A worker claims and dies without writing anything (kill -9).
    dead = await state.claim_next_branch(worker_id="dead", lease_ttl_s=0.2,
                                         run_prefix=plan.run_id)
    assert dead is not None and dead.lease_generation == 1
    await asyncio.sleep(0.35)

    await runtime.work(plan.run_id)
    branch = await state.get_branch(dead.branch_id)
    assert branch.status == "completed" and branch.lease_generation == 2

    # The zombie wakes up and tries to record its (stale) result.
    with pytest.raises(StaleFenceError):
        await state.record_iteration(
            run_id=plan.run_id, iteration=0, candidate="parent",
            row={"staged_blob": "zombie"}, branch_id=dead.branch_id,
            fence=dead.lease_generation,
        )
    rows = await state.list_iterations(run_id=plan.run_id)
    assert len(rows) == 1 and rows[0]["fence"] == 2
    results = await runtime.collect(plan)
    assert len(results) == 1
