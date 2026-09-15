"""Claude Code adapter against a fake ``claude`` binary, session forking,
and an opt-in live fork smoke test (``HARNESS_LIVE_CLAUDE=1``)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agents.base import HarnessBinding, ResumePoint  # noqa: E402
from app.agents.claude_code import (  # noqa: E402
    ClaudeCodeAdapter,
    fork_session,
    normalize_tool_event,
    session_dir_for,
    usage_tokens,
)
from app.agents.recorder import record_run  # noqa: E402
from app.continual.harness_object import (  # noqa: E402
    HarnessSnapshot,
    VerificationRule,
    render_context,
)
from app.continual.snapshots import SnapshotStore  # noqa: E402
from app.refinement.tasks import load_corpus  # noqa: E402

CORPUS = load_corpus()


def _binding(task) -> HarnessBinding:
    snap = HarnessSnapshot(verification=[VerificationRule(
        name="t", kind="targeted_tests", description="Run only the task's test file.")])
    return HarnessBinding(harness_id="h", memory_version="m", snapshot=snap, memory=[],
                          context=render_context(snap, [], task_text=task.instruction))


def _fake_claude(tmp_path: Path) -> Path:
    """A stand-in for `claude -p --output-format stream-json`: reads calc,
    edits it for real in cwd, runs a failing pytest, reports usage."""
    script = tmp_path / "fake-claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys
        args = sys.argv[1:]
        open(os.path.join({str(tmp_path)!r}, "argv.json"), "w").write(json.dumps(args))
        cwd = os.getcwd()
        def out(obj): print(json.dumps(obj), flush=True)
        out({{"type": "system", "subtype": "init", "model": args[args.index("--model") + 1],
             "session_id": "sess-1"}})
        target = os.path.join(cwd, "calculator.py")
        out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "a",
             "name": "Read", "input": {{"file_path": target}}}}]}}}})
        out({{"type": "user", "message": {{"content": [{{"type": "tool_result",
             "tool_use_id": "a", "content": open(target).read()}}]}}}})
        import time; time.sleep(0.5)  # model latency between tool calls
        with open(target, "a") as fh:
            fh.write("# edited\\n")
        out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "b",
             "name": "Edit", "input": {{"file_path": target, "old_string": "x", "new_string": "y"}}}}]}}}})
        out({{"type": "user", "message": {{"content": [{{"type": "tool_result",
             "tool_use_id": "b", "content": "ok"}}]}}}})
        out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "c",
             "name": "Bash", "input": {{"command": "pytest tests/test_calculator.py -q"}}}}]}}}})
        out({{"type": "user", "message": {{"content": [{{"type": "tool_result",
             "tool_use_id": "c", "content": "1 failed, 4 passed"}}]}}}})
        out({{"type": "result", "subtype": "success", "is_error": False, "result": "done",
             "usage": {{"input_tokens": 10, "cache_read_input_tokens": 1000,
                        "cache_creation_input_tokens": 200, "output_tokens": 50}}}})
        """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_normalize_maps_claude_tools_to_six_tool_vocabulary(tmp_path):
    cwd = tmp_path
    assert normalize_tool_event("Read", {"file_path": str(cwd / "a" / "b.py")}, "", cwd) == (
        "read_file", {"path": "a/b.py"}, False)
    tool, inp, err = normalize_tool_event("Bash", {"command": "pytest -q"}, "2 failed, 1 passed", cwd)
    assert (tool, inp["command"], err) == ("run_bash", "pytest -q", True)
    assert normalize_tool_event("Glob", {"pattern": "**/*.py"}, [], cwd)[0] == "grep_search"
    assert normalize_tool_event("Write", {"file_path": "/elsewhere/x.py"}, {}, cwd)[1] == {
        "path": "/elsewhere/x.py"}
    assert usage_tokens({"input_tokens": 1, "cache_read_input_tokens": 2,
                         "cache_creation_input_tokens": 3, "output_tokens": 4}) == 10


def test_adapter_records_steps_checkpoints_and_pinned_flags(tmp_path):
    task = CORPUS["task-001-fix-typo"]
    adapter = ClaudeCodeAdapter(model="claude-haiku-4-5-20251001",
                                claude_bin=str(_fake_claude(tmp_path)),
                                projects_dir=tmp_path / "projects")
    snaps = SnapshotStore(tmp_path / "snap")
    staged = asyncio.run(record_run(adapter, task, _binding(task), tmp_path / "ws", snaps, seed=0))

    assert staged.status == "completed"
    assert staged.model_version == "claude-haiku-4-5-20251001"
    tools = [s.tool for s in staged.steps]
    assert staged.steps[0].kind == "context"
    assert tools[1:] == ["read_file", "apply_patch", "run_bash", "task_complete"]
    assert staged.steps[1].input == {"path": "calculator.py"}
    assert staged.steps[3].is_error  # "1 failed"
    assert staged.steps[2].agent_state["tool_results"] == 2
    assert staged.steps[2].snapshot_id != staged.steps[1].snapshot_id  # the edit is captured
    assert staged.tokens == staged.context_tokens + 1260

    argv = json.loads((tmp_path / "argv.json").read_text())
    for flag in ("--setting-sources", "--strict-mcp-config", "--system-prompt-snapshot",
                 "--append-system-prompt", "--session-id"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5-20251001"
    assert "<HARNESS_CONTEXT>" in argv[argv.index("--append-system-prompt") + 1]


def test_fork_session_truncates_after_k_tool_results_and_rewrites_paths(tmp_path):
    projects = tmp_path / "projects"
    src_cwd, dst_cwd = tmp_path / "src", tmp_path / "dst"
    src_cwd.mkdir()
    dst_cwd.mkdir()
    sid = "11111111-1111-1111-1111-111111111111"
    rows = [
        {"type": "ai-title", "sessionId": sid},
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid, "cwd": str(src_cwd),
         "message": {"role": "user", "content": "fix it"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": sid,
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                  "input": {"file_path": f"{src_cwd}/calc.py"}}]}},
        {"type": "user", "uuid": "r1", "parentUuid": "a1", "sessionId": sid,
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "r1", "sessionId": sid,
         "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Edit",
                                  "input": {"file_path": f"{src_cwd}/calc.py"}}]}},
        {"type": "user", "uuid": "r2", "parentUuid": "a2", "sessionId": sid,
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
    ]
    src_dir = session_dir_for(src_cwd, projects)
    src_dir.mkdir(parents=True)
    (src_dir / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    new = "22222222-2222-2222-2222-222222222222"
    dest = fork_session(src_dir / f"{sid}.jsonl", cut_after_tool_results=1, src_cwd=str(src_cwd),
                        dst_cwd=str(dst_cwd), new_session_id=new, projects_dir=projects)
    kept = [json.loads(line) for line in dest.read_text().splitlines()]
    assert [r["uuid"] for r in kept] == ["u1", "a1", "r1"]
    assert dest.parent == session_dir_for(dst_cwd, projects)
    blob = dest.read_text()
    assert str(src_cwd) not in blob and str(dst_cwd) in blob
    assert sid not in blob and new in blob
    with pytest.raises(ValueError):
        fork_session(src_dir / f"{sid}.jsonl", cut_after_tool_results=5, src_cwd=str(src_cwd),
                     dst_cwd=str(dst_cwd), new_session_id=new, projects_dir=projects)


def test_fork_session_never_splits_a_parallel_tool_batch(tmp_path):
    """One assistant turn issued two tool calls; cutting after the first
    result must still include the second (a half-answered batch is not a
    valid conversation to resume)."""
    projects = tmp_path / "projects"
    cwd = tmp_path / "w"
    cwd.mkdir()
    sid = "33333333-3333-3333-3333-333333333333"
    rows = [
        {"type": "user", "uuid": "u1", "sessionId": sid, "message": {"content": "go"}},
        {"type": "assistant", "uuid": "a1", "sessionId": sid, "message": {"content": [
            {"type": "tool_use", "id": "p", "name": "Read", "input": {}}]}},
        {"type": "assistant", "uuid": "a2", "sessionId": sid, "message": {"content": [
            {"type": "tool_use", "id": "q", "name": "Read", "input": {}}]}},
        {"type": "user", "uuid": "r1", "sessionId": sid, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "p", "content": "1"}]}},
        {"type": "user", "uuid": "r2", "sessionId": sid, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "q", "content": "2"}]}},
        {"type": "assistant", "uuid": "a3", "sessionId": sid, "message": {"content": [
            {"type": "text", "text": "next"}]}},
    ]
    src = session_dir_for(cwd, projects)
    src.mkdir(parents=True)
    (src / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    dest = fork_session(src / f"{sid}.jsonl", cut_after_tool_results=1, src_cwd=str(cwd),
                        dst_cwd=str(cwd), new_session_id="44444444-4444-4444-4444-444444444444",
                        projects_dir=projects)
    assert [json.loads(x)["uuid"] for x in dest.read_text().splitlines()] == [
        "u1", "a1", "a2", "r1", "r2"]


@pytest.mark.skipif(os.environ.get("HARNESS_LIVE_CLAUDE") != "1",
                    reason="live Claude Code run; set HARNESS_LIVE_CLAUDE=1 (costs model usage)")
def test_live_claude_trajectory_forks_mid_run(tmp_path):
    task = CORPUS["task-001-fix-typo"]
    snaps = SnapshotStore(tmp_path / "snap")
    model = os.environ.get("HARNESS_LIVE_MODEL", "claude-haiku-4-5-20251001")
    parent = asyncio.run(record_run(ClaudeCodeAdapter(model=model, max_budget_usd=0.5), task,
                                    _binding(task), tmp_path / "parent", snaps, seed=0))
    assert parent.status == "completed", parent.error
    tool_steps = [s for s in parent.steps if s.kind == "tool_call" and s.agent_state]
    first = tool_steps[0]
    resume = ResumePoint(trajectory_id=parent.trajectory_id, step=first.step,
                         snapshot_id=first.snapshot_id, agent_state=first.agent_state,
                         prefix_tool_calls=first.step,
                         prefix_tokens=sum(s.tokens for s in parent.steps[: first.step + 1]))
    fork = asyncio.run(record_run(ClaudeCodeAdapter(model=model, max_budget_usd=0.5), task,
                                  _binding(task), tmp_path / "fork", snaps, seed=1, resume=resume))
    assert fork.status == "completed", fork.error
    assert fork.fork_step == first.step and fork.steps[0].step == first.step + 1
