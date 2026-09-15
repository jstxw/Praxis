"""Harness object H = (I, S, M, V, C): serialization, complexity, and
retrieve-and-rank context injection (ARCHITECTURE §2, §8)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.continual.harness_object import (  # noqa: E402
    Control,
    HarnessSnapshot,
    Instruction,
    MemoryEntry,
    Skill,
    VerificationRule,
    complexity_score,
    empty_harness,
    estimate_tokens,
    render_context,
)
from app.continual.snapshots import SnapshotStore  # noqa: E402


def _rich_harness(budget: int = 2000) -> HarnessSnapshot:
    return HarnessSnapshot(
        instructions=[Instruction(name="concise", text="Keep changes minimal.")],
        skills=[
            Skill(
                name="targeted_monorepo_testing",
                description="Run only the package's tests.",
                triggers=["test", "tests"],
                steps=["find the package", "run its tests"],
            ),
            Skill(name="parser_skill", description="Parsing tips.", triggers=["parser"]),
        ],
        verification=[
            VerificationRule(
                name="run-targeted", kind="targeted_tests",
                description="Run the task's test file, not the full suite.",
            )
        ],
        control=Control(context_budget_tokens=budget),
    )


def test_snapshot_round_trips_and_digest_is_content_addressed():
    h = _rich_harness()
    again = HarnessSnapshot.from_json(h.to_json())
    assert again == h
    assert again.digest() == h.digest()
    changed = HarnessSnapshot.from_json(h.to_json())
    changed.instructions.append(Instruction(name="x", text="y"))
    assert changed.digest() != h.digest()


def test_empty_harness_has_zero_prompt_complexity_and_injects_nothing():
    h = empty_harness()
    assert h.complexity()["prompt_tokens"] == 0
    ctx = render_context(h, [], task_text="fix the bug")
    assert ctx.text == "" and ctx.tokens == 0


def test_complexity_counts_components():
    c = _rich_harness().complexity()
    assert c["skill_count"] == 2 and c["policy_count"] == 1 and c["instruction_count"] == 1
    assert c["prompt_tokens"] > 0
    assert complexity_score(c) > 4


def test_context_injects_top_k_memory_and_triggered_skills_only():
    memory = [
        MemoryEntry(id="m-parser", text="parser.py raises ValueError on empty input",
                    keys=["parser.py"]),
        MemoryEntry(id="m-geo", text="geometry package uses radians", keys=["geometry"]),
    ]
    ctx = render_context(
        _rich_harness(), memory, task_text="Fix parser.py so tests pass"
    )
    assert ctx.memory_ids == ["m-parser"]  # geometry fact not retrieved
    assert "targeted_monorepo_testing" in ctx.skill_names
    assert "parser_skill" in ctx.skill_names
    assert ctx.text.startswith("<HARNESS_CONTEXT>") and ctx.text.endswith("</HARNESS_CONTEXT>")
    assert "Repository facts:" in ctx.text and "Verification:" in ctx.text
    assert ctx.tokens == estimate_tokens(ctx.text)


def test_context_budget_drops_lowest_priority_first():
    """Verification is admitted before memory, skills and instructions."""
    memory = [
        MemoryEntry(id=f"m{i}", text=f"tests fact {i} " + "x" * 200, keys=["tests"])
        for i in range(8)
    ]
    ctx = render_context(_rich_harness(budget=120), memory, task_text="make tests pass")
    assert ctx.rule_names == ["run-targeted"]
    assert ctx.dropped, "a tiny budget must drop something"
    assert "concise" in ctx.dropped  # instructions go first
    assert ctx.tokens <= 120 + 20  # budget respected (labels are approximate)


def test_snapshot_store_restore_is_exact_and_ids_are_content_addressed(tmp_path):
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "a.py").write_text("x = 1\n")
    (ws / "tests" / "test_a.py").write_text("def test(): pass\n")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "junk.pyc").write_bytes(b"\0")

    store = SnapshotStore(tmp_path / "snap")
    first = store.snapshot(ws)
    assert store.snapshot(ws) == first  # identical content → identical id

    (ws / "a.py").write_text("x = 2\n")
    (ws / "new.py").write_text("y = 1\n")
    second = store.snapshot(ws)
    assert second != first
    assert store.diff(first, second) == {
        "added": ["new.py"], "removed": [], "modified": ["a.py"],
    }

    restored = tmp_path / "restored"
    store.restore(first, restored)
    assert (restored / "a.py").read_text() == "x = 1\n"
    assert not (restored / "new.py").exists()
    assert not (restored / "__pycache__").exists()
    assert store.snapshot(restored) == first
