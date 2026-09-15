"""`harness` CLI: init → wrap hooks record a real trajectory → status.

Everything runs under a temporary ``HARNESS_HOME``; nothing touches
``~/.harness`` or the repo's experiments directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.harness_cli import app, hook_settings  # noqa: E402

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_PREREG_DIR", str(tmp_path / "prereg"))
    return tmp_path / "home"


def test_init_is_zero_config_and_idempotent(home, tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    result = runner.invoke(app, ["init", "--root", str(repo), "--agent", "claude"])
    assert result.exit_code == 0, result.output
    assert "Active: H0" in result.output
    assert (home / "state.db").exists() and (home / "experience.db").exists()
    project_file = json.loads((repo / ".harness" / "project.json").read_text())
    assert project_file["mode"] == "local"
    again = runner.invoke(app, ["init", "--root", str(repo)])
    assert "already initialized" in again.output


def test_wrap_hooks_record_a_trajectory_and_status_reports_it(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert runner.invoke(app, ["init", "--root", str(repo), "--agent", "claude",
                               "--test-command", "true"]).exit_code == 0

    base = {"session_id": "s-1", "cwd": str(repo)}
    prompt = runner.invoke(app, ["hook", "prompt"],
                           input=json.dumps({**base, "prompt": "fix the bug in pager.py"}))
    assert prompt.exit_code == 0
    assert prompt.output == ""  # empty H0 injects nothing
    for name, tool_input, response in [
        ("Read", {"file_path": str(repo / "pager.py")}, {"content": "x"}),
        ("Read", {"file_path": str(repo / "pager.py")}, {"content": "x"}),
        ("Edit", {"file_path": str(repo / "pager.py")}, {}),
        ("Bash", {"command": "pytest -q"}, {"stdout": "1 passed"}),
    ]:
        r = runner.invoke(app, ["hook", "tool"], input=json.dumps(
            {**base, "tool_name": name, "tool_input": tool_input, "tool_response": response}))
        assert r.exit_code == 0
    assert runner.invoke(app, ["hook", "stop"], input=json.dumps(base)).exit_code == 0
    assert not (home / "hook-errors.log").exists()

    status = runner.invoke(app, ["status", "--root", str(repo)])
    assert status.exit_code == 0, status.output
    assert "Active:  H0" in status.output
    assert "Success       100.0%   (observed, n=1; not a comparison)" in status.output
    assert "Tool calls    4.0" in status.output
    assert "Noise floor: UNMEASURED" in status.output
    assert "Last promotion: none" in status.output


def test_hook_never_breaks_the_session_on_bad_input(home):
    result = runner.invoke(app, ["hook", "tool"], input="{not json")
    assert result.exit_code == 0
    assert (home / "hook-errors.log").exists()


def test_wrap_dry_run_passes_through_args_with_hook_settings(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner.invoke(app, ["init", "--root", str(repo)])
    result = runner.invoke(app, ["wrap", "claude", "--root", str(repo), "--dry-run",
                                 "--model", "opus"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("claude --settings ")
    assert "--model opus" in result.output
    settings = json.loads((home / "wrap-settings.json").read_text())
    assert set(settings["hooks"]) == {"UserPromptSubmit", "PostToolUse", "Stop"}
    assert hook_settings("h")["hooks"]["Stop"][0]["hooks"][0]["command"] == "h hook stop"


def test_task_sets_are_disjoint_and_printed(home):
    result = runner.invoke(app, ["tasks", "sets"])
    assert result.exit_code == 0
    sets = json.loads(result.output)
    assert not set(sets["trigger"]) & set(sets["holdout"])
    assert len(sets["holdout"]) == 10


def test_experiment_refuses_to_run_before_preregistration(home):
    result = runner.invoke(app, ["experiment", "e1", "--agent", "synthetic"])
    assert result.exit_code == 2
    assert "not pre-registered" in result.output
    registered = runner.invoke(app, ["experiment", "e1", "--agent", "synthetic", "--register"])
    assert registered.exit_code == 0, registered.output
    assert "pre-registered E1" in registered.output
