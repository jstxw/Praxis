"""``harness`` — the one-binary CLI (VISION §2).

    harness init                 set up ~/.harness and register this project (H0)
    harness wrap claude          run Claude Code with harness context + trajectory hooks
    harness status               active harness, observed numbers, gate-cleared promotions
    harness run --task ID        run one corpus task under the active harness
    harness refine               one refinement cycle (policy → reflect → evaluate → gate)
    harness experiment e1 …      pre-registered experiments E0–E5
    harness tasks check|sets     corpus hygiene and the fixed task sets
    harness mcp                  the durable-execution MCP server (6 tools)
    harness sim                  deterministic simulation (both store backings)

Local mode (default): SQLite state + experience store under
``$HARNESS_HOME`` (``~/.harness``), in-process workers. ``--service``
uses Postgres for both. Nothing is written outside the project directory
and ``~/.harness``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import typer

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.refinement.config import HarnessConfig, NoiseFloorUnmeasured, harness_home  # noqa: E402

app = typer.Typer(
    name="harness",
    help="A coding-agent harness that improves through use — only when the evidence clears the gate.",
    no_args_is_help=True,
)
tasks_app = typer.Typer(help="Task corpus hygiene and fixed task sets.", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")

DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
AGENTS = ("synthetic", "claude")
PROJECT_FILE = ".harness/project.json"


# ─────────────────────────────────────────────────────────────────────
# Wiring
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Stores:
    state: Any
    xp: Any
    snapshots: Any
    mode: str


@asynccontextmanager
async def open_stores(*, service: bool = False) -> AsyncIterator[Stores]:
    from app.continual.experience import ExperienceStore
    from app.continual.snapshots import SnapshotStore
    from app.meta_harness.sqlite_store import SQLiteStateStore

    home = harness_home()
    home.mkdir(parents=True, exist_ok=True)
    if service:
        from app.meta_harness.persistence import get_dsn
        from app.meta_harness.store import PostgresStateStore

        state = await PostgresStateStore.connect(get_dsn())
        xp = await ExperienceStore.postgres(get_dsn())
        mode = "service"
    else:
        state = SQLiteStateStore(home / "state.db")
        xp = ExperienceStore.sqlite(home / "experience.db")
        mode = "local"
    await state.setup()
    await xp.setup()
    try:
        yield Stores(state=state, xp=xp, snapshots=SnapshotStore(home / "snapshots"), mode=mode)
    finally:
        await xp.close()
        await state.close()


def agent_model(agent: str, model: str | None) -> str:
    if agent == "synthetic":
        from app.agents.synthetic import MODEL_VERSION

        return MODEL_VERSION
    return model or DEFAULT_CLAUDE_MODEL


def adapter_factory(agent: str, model: str | None):
    if agent == "synthetic":
        from app.agents.synthetic import SyntheticAgent

        return SyntheticAgent
    if agent == "claude":
        from app.agents.claude_code import ClaudeCodeAdapter

        pinned = agent_model(agent, model)
        return lambda: ClaudeCodeAdapter(model=pinned)
    raise typer.BadParameter(f"--agent must be one of {AGENTS}")


def build_runtime(stores: Stores, agent: str, model: str | None, *, tasks=None):
    from app.refinement.evaluation import EvaluationRuntime
    from app.refinement.evaluator import SandboxUnavailable, Verifier
    from app.refinement.tasks import load_corpus

    try:
        verifier = Verifier.docker()
    except SandboxUnavailable as exc:
        typer.echo(f"refusing to evaluate: {exc}", err=True)
        raise typer.Exit(3) from None
    return EvaluationRuntime(
        state=stores.state, xp=stores.xp, snapshots=stores.snapshots,
        tasks=tasks or load_corpus(), adapter_factory=adapter_factory(agent, model),
        verifier=verifier, work_root=harness_home() / "work",
        lease_ttl_s=120.0 if agent == "claude" else 30.0,
    )


def _run(coro):
    return asyncio.run(coro)


def find_project_root(start: Path) -> Path:
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / PROJECT_FILE).exists():
            return candidate
    return start


async def load_project(stores: Stores, root: Path):
    project = await stores.xp.get_project(root=str(root.resolve()))
    if project is None:
        typer.echo(f"no harness project at {root} — run `harness init` first", err=True)
        raise typer.Exit(2)
    return project


# ─────────────────────────────────────────────────────────────────────
# init / status
# ─────────────────────────────────────────────────────────────────────


def detect_test_command(root: Path) -> str | None:
    if (root / "pytest.ini").exists() or (root / "tests").is_dir() or (root / "pyproject.toml").exists():
        return "pytest -q"
    if (root / "package.json").exists():
        return "npm test --silent"
    return None


@app.command()
def init(
    root: Path = typer.Option(Path("."), help="Project root"),
    agent: str = typer.Option("claude", help=f"Agent: {', '.join(AGENTS)}"),
    model: str = typer.Option(None, help="Pinned model version (claude)"),
    test_command: str = typer.Option(None, help="Project verification command"),
    service: bool = typer.Option(False, "--service", help="Postgres instead of SQLite"),
) -> None:
    """Register this project with an empty harness H0. Zero config."""
    from app.continual.experience import HarnessEventRecord, ProjectRecord
    from app.continual.harness_object import HarnessSnapshot

    root = root.resolve()

    async def go():
        async with open_stores(service=service) as stores:
            existing = await stores.xp.get_project(root=str(root))
            if existing is not None:
                typer.echo(f"already initialized: {root} (project {existing.id[:8]})")
                return existing
            memory = await stores.xp.create_memory_version([])
            h0 = await stores.xp.create_harness(HarnessSnapshot(), memory_version=memory.id,
                                                status="active", label="H0")
            config = {
                "test_command": test_command or detect_test_command(root),
                "model_version": agent_model(agent, model),
            }
            project = await stores.xp.create_project(ProjectRecord(
                root=str(root), agent=agent, active_harness=h0.id, h0_harness=h0.id,
                memory_version=memory.id, config=config,
            ))
            await stores.xp.record_harness_event(HarnessEventRecord(
                project_id=project.id, kind="init", harness_id=h0.id,
                summary="initialized with empty harness H0",
            ))
            return project

    project = _run(go())
    (root / ".harness").mkdir(exist_ok=True)
    (root / PROJECT_FILE).write_text(json.dumps({
        "project_id": project.id, "agent": project.agent,
        "state": str(harness_home()), "mode": "service" if service else "local",
    }, indent=2) + "\n")
    typer.echo(f"Project: {root.name}   Agent: {project.agent}   Active: H0")
    typer.echo(f"state: {harness_home()}   verification: {project.config.get('test_command')}")
    typer.echo("next: harness wrap claude")


async def status_text(stores: Stores, root: Path) -> str:
    """VISION §2 status. Shows only deltas that cleared the gate (rule 6)."""
    from app.refinement.policy import TASKS_BETWEEN_REFLECTIONS, is_observed_work

    project = await load_project(stores, root)
    active = await stores.xp.get_harness(project.active_harness)
    events = await stores.xp.list_harness_events(project.id)
    promotions = [e for e in events if e.kind == "promote"]
    last_promo_at = promotions[-1].created_at if promotions else 0.0

    pairs = [(t, e) for t, e in await stores.xp.evaluated_trajectories(
        harness_id=project.active_harness, agent=project.agent) if is_observed_work(t)]
    since_promo = [p for p in pairs if p[0].created_at > last_promo_at]
    reflections = await stores.xp.list_reflections(project_id=project.id)
    last_refl = reflections[-1].created_at if reflections else 0.0
    since_refl = len([p for p in pairs if p[0].created_at > last_refl])

    lines = [
        f"Project: {Path(project.root).name:<18}Agent: {project.agent}"
        f"  ({project.config.get('model_version', '?')})",
        f"Active:  {active.label or active.id[:8]:<18}Tasks since promotion: {len(since_promo)}",
        "",
    ]
    if pairs:
        n = len(pairs)
        success = sum(1 for _, e in pairs if e.success and not e.voided) / n
        calls = sum((e.tool_calls or 0) for _, e in pairs) / n
        lines += [
            f"Success       {success:.1%}   (observed, n={n}; not a comparison)",
            f"Tool calls    {calls:.1f}",
        ]
    else:
        lines.append("No observed tasks under the active harness yet.")
    lines.append("")
    if promotions:
        promo = promotions[-1]
        parent = await stores.xp.get_harness(promo.from_harness) if promo.from_harness else None
        lines.append(f"Last promotion: {(parent.label if parent else '?')} → {active.label}")
        lines.append(f"  {promo.summary}")
        evaluation = promo.details.get("evaluation", {})
        audit = evaluation.get("audit", {}).get("holdout", {})
        if audit:
            lines.append(f"  Gate: cleared on holdout · runs {audit.get('runs')} · "
                         f"isolation {','.join(audit.get('isolation', []))}")
    else:
        lines.append("Last promotion: none (no candidate has cleared the gate)")
    drift = [e for e in events if e.kind == "drift_check"]
    if drift:
        verdict = "DRIFT DETECTED" if drift[-1].details.get("drifted") else "no drift detected"
        lines.append(f"Drift check vs H0: {verdict} (report only; not a gate-cleared delta)")
    rejects = [e for e in events if e.kind == "reject"]
    if rejects:
        lines.append(f"Rejected cycles: {len(rejects)} (latest: {rejects[-1].summary})")
    lines.append("")
    config = HarnessConfig.load()
    try:
        nf = config.noise_floor(project.agent, project.config.get("model_version", "?"))
        lines.append(f"Noise floor: measured ({nf.experiment_file})")
    except NoiseFloorUnmeasured:
        lines.append("Noise floor: UNMEASURED — no promotion possible until "
                     "`harness experiment e1` runs for this agent/model")
    lines.append(f"Next reflection: {max(0, TASKS_BETWEEN_REFLECTIONS - since_refl)} tasks")
    return "\n".join(lines)


@app.command()
def status(
    root: Path = typer.Option(Path("."), help="Project root"),
    service: bool = typer.Option(False, "--service"),
) -> None:
    """Active harness, observed numbers, and gate-cleared promotions only."""

    async def go():
        async with open_stores(service=service) as stores:
            return await status_text(stores, find_project_root(root))

    typer.echo(_run(go()))


# ─────────────────────────────────────────────────────────────────────
# run / refine
# ─────────────────────────────────────────────────────────────────────


@app.command()
def run(
    task: str = typer.Option(..., "--task", help="Corpus task id"),
    root: Path = typer.Option(Path("."), help="Project root"),
    seed: int = typer.Option(0),
    model: str = typer.Option(None),
    service: bool = typer.Option(False, "--service"),
) -> None:
    """Run one corpus task under the project's active harness (observed work)."""
    from app.refinement.loop import RefinementLoop
    from app.refinement.tasks import load_corpus, split_task_sets

    async def go():
        async with open_stores(service=service) as stores:
            project = await load_project(stores, find_project_root(root))
            tasks = load_corpus()
            runtime = build_runtime(stores, project.agent, model, tasks=tasks)
            loop = RefinementLoop(
                xp=stores.xp, runtime=runtime, tasks=tasks,
                task_sets=split_task_sets(tasks, seed=20260915), config=HarnessConfig.load(),
                agent=project.agent, model_version=project.config.get("model_version"),
            )
            result = await loop.run_task(project, task, seed=seed)
            return result

    try:
        result = _run(go())
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    ev = result.evaluation
    typer.echo(json.dumps({
        "task": task, "result": result.trajectory.result, "success": ev.success,
        "tool_calls": ev.tool_calls, "tokens": ev.tokens, "redundant_reads": ev.redundant_reads,
        "voided": ev.voided, "isolation": ev.isolation, "trajectory": result.trajectory.id,
    }, indent=2))


@app.command()
def refine(
    root: Path = typer.Option(Path("."), help="Project root"),
    observe: int = typer.Option(0, help="First run N trigger-set corpus tasks as observed work"),
    workers: int = typer.Option(2),
    model: str = typer.Option(None),
    seed: int = typer.Option(20260915),
    service: bool = typer.Option(False, "--service"),
) -> None:
    """One refinement cycle: policy → reflection → candidates → gate."""
    from app.refinement.loop import RefinementLoop
    from app.refinement.tasks import load_corpus, split_task_sets

    async def go():
        async with open_stores(service=service) as stores:
            project = await load_project(stores, find_project_root(root))
            tasks = load_corpus()
            sets = split_task_sets(tasks, seed=20260915)
            runtime = build_runtime(stores, project.agent, model, tasks=tasks)
            loop = RefinementLoop(
                xp=stores.xp, runtime=runtime, tasks=tasks, task_sets=sets,
                config=HarnessConfig.load(), agent=project.agent,
                model_version=project.config.get("model_version"),
                n_workers=workers, seed=seed,
            )
            for i in range(observe):
                task_id = sets.trigger[i % len(sets.trigger)]
                r = await loop.run_task(project, task_id, seed=seed + i)
                typer.echo(f"  observed {task_id}: {r.trajectory.result} "
                           f"({r.evaluation.tool_calls} tool calls)")
            outcome = await loop.cycle(project)
            return outcome

    outcome = _run(go())
    typer.echo(f"cycle: {outcome.status} — {outcome.reason} ({outcome.wall_time_s}s)")
    for verdict in outcome.verdicts:
        mark = "PROMOTE" if verdict["promotable"] else "reject "
        typer.echo(f"  {mark} {verdict['arm']}: " + ("; ".join(verdict["reasons"]) or "cleared the gate"))


# ─────────────────────────────────────────────────────────────────────
# experiments / tasks
# ─────────────────────────────────────────────────────────────────────


@app.command()
def experiment(
    name: str = typer.Argument(..., help="e0 | e1 | e2 | e3 | e4 | e5"),
    agent: str = typer.Option("synthetic"),
    model: str = typer.Option(None),
    reps: int = typer.Option(5),
    workers: int = typer.Option(2),
    register: bool = typer.Option(False, "--register", help="Write the pre-registration and exit"),
    labels: list[Path] = typer.Option(None, "--labels", help="E3 label files (two)"),
    template: Path = typer.Option(None, "--template", help="E3: write a label template here"),
    seed: int = typer.Option(20260915),
    n_resamples: int = typer.Option(10_000),
    service: bool = typer.Option(False, "--service"),
) -> None:
    """Run a pre-registered experiment. Register first; results never precede it."""
    from app.refinement import experiments as ex
    from app.refinement.tasks import load_corpus, split_task_sets

    exp = name.upper()
    if exp not in {"E0", "E1", "E2", "E3", "E4", "E5"}:
        raise typer.BadParameter("experiment must be e0..e5")
    command = "harness " + " ".join(shlex.quote(a) for a in sys.argv[1:])

    async def go():
        async with open_stores(service=service) as stores:
            tasks = load_corpus()
            sets = split_task_sets(tasks, seed=20260915)
            config = HarnessConfig.load()
            runtime = None if (register or exp == "E3") else build_runtime(stores, agent, model, tasks=tasks)
            ctx = ex.ExperimentContext(
                xp=stores.xp, state=stores.state, runtime=runtime, tasks=tasks, sets=sets,
                config=config, agent=agent, model_version=agent_model(agent, model),
                command=command.replace("--register", "").strip(), n_workers=workers,
                seed=seed, n_resamples=n_resamples,
            )
            if template is not None:
                template.write_text(json.dumps(await ex.e3_label_template(ctx), indent=2))
                return f"wrote label template {template}"
            prereg = ex.ensure_preregistered(exp, ctx, register=register)
            if register:
                return f"pre-registered {exp}: {ex.display_path(prereg)}"
            if exp == "E3":
                path = await ex.run_e3(ctx, preregistration=prereg, label_files=list(labels or []))
            elif exp == "E0":
                path = await ex.run_e0(ctx, preregistration=prereg)
            else:
                path = await ex.RUNNERS[exp](ctx, preregistration=prereg, reps=reps)
            return f"{exp} result: {ex.display_path(path)}"

    try:
        typer.echo(_run(go()))
    except ex.PreregistrationMismatch as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    except NoiseFloorUnmeasured as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


@tasks_app.command("check")
def tasks_check(k: int = typer.Option(3, help="Gold patch repetitions")) -> None:
    """Gold patch test (k×) and must-fail-at-base, in the docker verifier."""
    from app.refinement.evaluator import SandboxUnavailable, Verifier
    from app.refinement.tasks import check_task_hygiene, load_corpus

    try:
        verifier = Verifier.docker()
    except SandboxUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from None
    tasks = load_corpus()
    failed = 0
    for task in tasks.values():
        result = check_task_hygiene(task, verifier, k=k)
        failed += not result.ok
        typer.echo(f"{'ok  ' if result.ok else 'FAIL'} {task.id} gold {result.gold_passes}/{k} "
                   f"{'; '.join(result.notes)}")
    typer.echo(f"{len(tasks)} tasks, {failed} failed hygiene (docker verifier, k={k})")
    raise typer.Exit(1 if failed else 0)


@tasks_app.command("sets")
def tasks_sets(seed: int = typer.Option(20260915)) -> None:
    """The fixed, disjoint trigger / holdout / regression split."""
    from app.refinement.tasks import load_corpus, split_task_sets

    typer.echo(json.dumps(split_task_sets(load_corpus(), seed=seed).to_json(), indent=2))


# ─────────────────────────────────────────────────────────────────────
# wrap / hook — observing real work in Claude Code
# ─────────────────────────────────────────────────────────────────────


def hook_settings(harness_cmd: str) -> dict[str, Any]:
    def entry(event: str, timeout: int = 60) -> list[dict[str, Any]]:
        return [{"hooks": [{"type": "command", "command": f"{harness_cmd} hook {event}",
                            "timeout": timeout}]}]

    return {
        "hooks": {
            "UserPromptSubmit": entry("prompt"),
            "PostToolUse": [{"matcher": "", "hooks": [
                {"type": "command", "command": f"{harness_cmd} hook tool"}]}],
            "Stop": entry("stop", timeout=900),  # runs the project's verification
        }
    }


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def wrap(
    ctx: typer.Context,
    agent: str = typer.Argument("claude"),
    root: Path = typer.Option(Path("."), help="Project root"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command instead of running"),
) -> None:
    """Run your coding agent with the harness around it. Workflow unchanged."""
    if agent != "claude":
        typer.echo("only `harness wrap claude` is supported: a stdout-only wrapper cannot "
                   "produce the per-tool events reflection needs", err=True)
        raise typer.Exit(2)
    root = find_project_root(root)
    harness_cmd = os.environ.get("HARNESS_BIN") or shlex.quote(sys.argv[0])
    settings_path = harness_home() / "wrap-settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(hook_settings(harness_cmd), indent=2))
    argv = ["claude", "--settings", str(settings_path), *ctx.args]
    if dry_run:
        typer.echo(" ".join(shlex.quote(a) for a in argv))
        return
    raise typer.Exit(subprocess.call(argv, cwd=root))


@app.command(hidden=True)
def hook(event: str = typer.Argument(...)) -> None:
    """Claude Code hook endpoint. Never blocks or breaks the agent."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        output = _run(handle_hook(event, payload))
        if output:
            sys.stdout.write(output)
    except Exception as exc:  # noqa: BLE001 — a hook must never break the session
        log = harness_home() / "hook-errors.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            fh.write(f"{time.time():.0f} {event}: {type(exc).__name__}: {exc}\n")


def _session_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    return harness_home() / "sessions" / f"{safe}.json"


async def handle_hook(event: str, payload: dict[str, Any]) -> str:
    """prompt → start task + inject context · tool → record step · stop → verify + record."""
    from app.agents.claude_code import normalize_tool_event
    from app.continual.experience import (
        EvaluationRecord,
        StepRecord,
        TrajectoryRecord,
    )
    from app.continual.harness_object import render_context
    from app.refinement.policy import compute_stats, should_reflect

    session_id = str(payload.get("session_id", "unknown"))
    cwd = Path(payload.get("cwd") or ".")
    state_file = _session_file(session_id)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    session = json.loads(state_file.read_text()) if state_file.exists() else {}

    root = find_project_root(cwd)
    project_file = root / PROJECT_FILE
    service = (
        project_file.exists() and json.loads(project_file.read_text()).get("mode") == "service"
    )
    async with open_stores(service=service) as stores:
        project = await stores.xp.get_project(root=str(root))
        if project is None:
            return ""
        if event == "prompt":
            harness = await stores.xp.get_harness(project.active_harness)
            memory = await stores.xp.get_memory_version(project.memory_version)
            context = render_context(harness.snapshot, memory.entries if memory else [],
                                     task_text=str(payload.get("prompt", "")))
            n = int(session.get("task_index", 0)) + 1
            session.update({
                "task_index": n, "prompt": payload.get("prompt", ""), "steps": [],
                "harness_id": harness.id, "memory_version": project.memory_version,
                "context": context.to_json(), "context_text": context.text,
                "started_at": time.time(),
            })
            state_file.write_text(json.dumps(session))
            return context.text  # UserPromptSubmit stdout is added as context
        if event == "tool" and session:
            tool, tool_input, is_error = normalize_tool_event(
                str(payload.get("tool_name", "")), payload.get("tool_input") or {},
                payload.get("tool_response"), cwd,
            )
            session["steps"].append({"tool": tool, "input": tool_input, "is_error": is_error,
                                     "ts": time.time()})
            state_file.write_text(json.dumps(session))
            return ""
        if event == "stop" and session and session.get("steps") is not None:
            steps = [StepRecord(step=0, kind="context", input={"context": session["context"]},
                                output=session.get("context_text", ""),
                                tokens=session["context"].get("tokens", 0))]
            for i, s in enumerate(session["steps"], 1):
                steps.append(StepRecord(step=i, kind="tool_call", tool=s["tool"], input=s["input"],
                                        is_error=s["is_error"], ts=s["ts"]))
            test_command = project.config.get("test_command")
            success, output = None, ""
            if test_command:
                try:
                    proc = subprocess.run(test_command, shell=True, cwd=project.root,
                                          capture_output=True,
                                          text=True, timeout=600)
                    success, output = proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
                except subprocess.TimeoutExpired:
                    success, output = False, "[verification timeout]"
            trajectory = TrajectoryRecord(
                branch_run_id=f"wrap-{session_id}-{session['task_index']}",
                harness_id=session["harness_id"], memory_version=session["memory_version"],
                task_id=f"wrap:{session.get('prompt', '')[:80]}", agent=project.agent,
                result="success" if success else "failure",
                model_version=project.config.get("model_version", "unknown"),
                tool_calls=len(session["steps"]), context_tokens=session["context"].get("tokens"),
                metadata={"session_id": session_id, "prompt": session.get("prompt"),
                          "verification_output": output},
            )
            await stores.xp.record_trajectory(trajectory, steps)
            from app.refinement.evaluator import count_redundant_reads

            await stores.xp.record_evaluation(EvaluationRecord(
                trajectory_id=trajectory.id, success=success,
                isolation="host (project verification command; not sandboxed)",
                tool_calls=len(session["steps"]), redundant_reads=count_redundant_reads(steps),
                audit={"verification_command": test_command},
            ))
            session["steps"] = None
            state_file.write_text(json.dumps(session))
            reflections = await stores.xp.list_reflections(project_id=project.id)
            stats = await compute_stats(
                stores.xp, harness_id=project.active_harness, agent=project.agent,
                last_reflection_at=reflections[-1].created_at if reflections else None,
            )
            decision = should_reflect(stats, noise_floor_success=None)
            if decision.reflect:
                notice = harness_home() / "reflection-due"
                notice.write_text(f"{project.root}: {decision.reason}\n")
            return ""
    return ""


# ─────────────────────────────────────────────────────────────────────
# mcp / sim
# ─────────────────────────────────────────────────────────────────────


@app.command()
def mcp() -> None:
    """The durable-execution MCP server: `claude mcp add harness -- harness mcp`."""
    from app.mcp_server import main as mcp_main

    mcp_main()


@app.command()
def sim(
    seeds: int = typer.Option(1000),
    backend: str = typer.Option("sqlite", help="memory | sqlite"),
) -> None:
    """Deterministic simulation of I1–I7 against a store backing."""
    from sim.run import main as sim_main

    raise typer.Exit(sim_main(["--seeds", str(seeds), "--backend", backend]))


def main() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))  # `sim` lives next to `app`
    app()


if __name__ == "__main__":
    main()
