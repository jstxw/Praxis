"""Claude Code adapter — the agent with the richest event stream.

Runs ``claude -p --output-format stream-json`` in the task workspace and
maps every tool call onto the six-tool vocabulary, so reflection sees
one language regardless of agent.

Reproducibility and isolation choices (each flag is load-bearing):

- ``--model <pinned full name>`` — never an alias; drift under a stable
  alias silently invalidates comparisons (DESIGN §1).
- ``--setting-sources local`` + ``--strict-mcp-config`` — no user
  settings, hooks, plugins or MCP servers leak into evaluation runs;
  the tool set is fixed by ``--allowedTools``.
- ``--permission-mode acceptEdits`` with Bash restricted to test and
  listing commands; anything else is denied, not prompted.
- ``--system-prompt-snapshot off`` — the harness context is re-rendered
  on resume, so a fork runs under *its* arm's harness, not the prefix's.

**Forking a real trajectory.** Claude Code persists each session as a
JSONL chain under ``~/.claude/projects/<slug(cwd)>/<session>.jsonl``.
The checkpoint after tool call ``k`` is ``(workspace snapshot, session
id, k, cwd)``. A fork copies the source session truncated right after
its ``k``-th tool result, rewrites the source workspace path to the
fork's workspace (so the model's history points at the fork, never the
original), and resumes it with ``--resume``. Verified end to end on
2026-09-15: the resumed agent continued mid-trajectory in the fork
workspace (see docs/REFINEMENT.md).

Limitations stated plainly: the agent needs network for the model API,
so it runs as a host process confined by Claude Code's own permissions
— the *verifier* is the Docker-sandboxed part. And the fork adds one
"continue" user turn, identically for every arm.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from app.agents.base import (
    TOOL_BASH,
    TOOL_COMPLETE,
    TOOL_GREP,
    TOOL_PATCH,
    TOOL_READ,
    TOOL_WRITE,
    AgentEvent,
    HarnessBinding,
    ResumePoint,
    TaskSpec,
)

CLAUDE_TOOL_MAP = {
    "Read": TOOL_READ,
    "Edit": TOOL_PATCH,
    "MultiEdit": TOOL_PATCH,
    "NotebookEdit": TOOL_PATCH,
    "Write": TOOL_WRITE,
    "Bash": TOOL_BASH,
    "Grep": TOOL_GREP,
    "Glob": TOOL_GREP,
    "LS": TOOL_GREP,
}
DEFAULT_ALLOWED_TOOLS = (
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(pytest *)", "Bash(python -m pytest *)", "Bash(python3 -m pytest *)", "Bash(ls *)",
)


def agent_env() -> dict[str, str]:
    """The agent's environment: this interpreter's bin dir first on PATH.

    Found on the first live run: on a host with no ``python`` and no
    approvable ``python3 -m pytest``, the agent could not run tests at
    all — an environmental failure that would otherwise masquerade as an
    "unverified submit" harness lesson. The interpreter running the
    harness has pytest installed, so its ``python``/``pytest`` are what
    the agent gets.
    """
    import sys

    env = dict(os.environ)
    bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env
CONTINUE_PROMPT = "Continue the task from where you left off."
_PYTEST_FAILED = re.compile(r"\b(\d+ failed|\d+ errors?|error collecting|ERROR )", re.IGNORECASE)


def claude_projects_dir() -> Path:
    return Path(os.environ.get("HARNESS_CLAUDE_PROJECTS", str(Path.home() / ".claude" / "projects")))


def session_dir_for(cwd: str | Path, projects_dir: Path | None = None) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(str(cwd)))
    return (projects_dir or claude_projects_dir()) / slug


def _rel(path: Any, cwd: Path) -> str:
    if not path:
        return ""
    try:
        return Path(os.path.realpath(str(path))).relative_to(os.path.realpath(str(cwd))).as_posix()
    except ValueError:
        return str(path)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    if isinstance(content, dict):
        return json.dumps(content)[:4000]
    return "" if content is None else str(content)


def normalize_tool_event(
    name: str, tool_input: dict[str, Any], tool_response: Any, cwd: Path
) -> tuple[str, dict[str, Any], bool]:
    """Claude tool call → (six-tool name, normalized input, is_error)."""
    tool = CLAUDE_TOOL_MAP.get(name, name.lower())
    if name in {"Read", "Edit", "MultiEdit", "Write", "NotebookEdit"}:
        normalized: dict[str, Any] = {"path": _rel(tool_input.get("file_path")
                                                   or tool_input.get("notebook_path"), cwd)}
        if name == "Read" and tool_input.get("offset"):
            normalized["offset"] = tool_input.get("offset")
    elif name == "Bash":
        normalized = {"command": str(tool_input.get("command", ""))}
    elif name in {"Grep", "Glob", "LS"}:
        normalized = {"pattern": str(tool_input.get("pattern", "")),
                      "path": _rel(tool_input.get("path"), cwd)}
    else:
        normalized = {k: v for k, v in tool_input.items() if isinstance(v, (str, int, float, bool))}

    is_error = False
    if isinstance(tool_response, dict):
        is_error = bool(tool_response.get("is_error") or tool_response.get("interrupted"))
        body = _text_of(tool_response.get("stdout") or tool_response.get("content"))
    else:
        body = _text_of(tool_response)
    if tool == TOOL_BASH and "pytest" in normalized.get("command", "") and _PYTEST_FAILED.search(body):
        is_error = True
    return tool, normalized, is_error


def fork_session(
    src_session_file: Path,
    *,
    cut_after_tool_results: int,
    src_cwd: str,
    dst_cwd: str,
    new_session_id: str,
    projects_dir: Path | None = None,
) -> Path:
    """Copy a session truncated after its k-th tool result into ``dst_cwd``."""
    rows = [json.loads(line) for line in src_session_file.read_text().splitlines() if line.strip()]
    old_session_id = next((r.get("sessionId") for r in rows if r.get("sessionId")), None)
    kept: list[dict[str, Any]] = []
    seen = 0
    open_tool_uses: set[str] = set()
    cutting = False
    for row in rows:
        if "uuid" not in row:
            continue  # titles, queue ops, summaries: not part of the chain
        message = row.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        results = [b.get("tool_use_id") for b in blocks if b.get("type") == "tool_result"]
        if cutting:
            # Never leave a parallel batch half-answered: keep only the
            # remaining results for tool calls that are still open.
            if row.get("type") == "user" and results and set(results) <= open_tool_uses:
                kept.append(row)
                open_tool_uses -= set(results)
                if not open_tool_uses:
                    break
                continue
            break
        kept.append(row)
        if row.get("type") == "assistant":
            open_tool_uses |= {b.get("id") for b in blocks if b.get("type") == "tool_use"}
        if row.get("type") == "user" and results:
            open_tool_uses -= set(results)
            seen += 1
            if seen == cut_after_tool_results:
                if not open_tool_uses:
                    break
                cutting = True
    if seen < cut_after_tool_results:
        raise ValueError(
            f"session {src_session_file.name} has {seen} tool results; cannot cut after "
            f"{cut_after_tool_results}"
        )
    # ensure_ascii=False: a non-ASCII workspace path must match literally.
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in kept)
    text = text.replace(os.path.realpath(src_cwd), os.path.realpath(dst_cwd))
    if old_session_id:
        text = text.replace(old_session_id, new_session_id)
    dest_dir = session_dir_for(dst_cwd, projects_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{new_session_id}.jsonl"
    dest.write_text(text + "\n")
    return dest


def usage_tokens(usage: dict[str, Any] | None) -> int:
    """Context processed + generated. Cache reads count: caching changes
    cost, not how much context the harness consumed (DESIGN §10)."""
    if not usage:
        return 0
    return int(
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("output_tokens") or 0)
    )


class ClaudeCodeAdapter:
    name = "claude"
    supports_fork = True
    reports_usage_at_end = True  # usage arrives only in the final `result` event

    def __init__(
        self,
        *,
        model: str,
        claude_bin: str = "claude",
        allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS,
        max_budget_usd: float = 1.0,
        projects_dir: Path | None = None,
    ) -> None:
        self.model = model
        self.claude_bin = claude_bin
        self.allowed_tools = allowed_tools
        self.max_budget_usd = max_budget_usd
        self.projects_dir = projects_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._cwd = ""
        self._session_id = ""
        self._tool_results = 0
        self._argv: list[str] = []

    async def start_task(
        self,
        task: TaskSpec,
        workspace: Path,
        harness: HarnessBinding,
        *,
        resume: ResumePoint | None = None,
        seed: int = 0,  # a hosted model gives no seed control; recorded, unused
    ) -> str:
        if shutil.which(self.claude_bin) is None and not Path(self.claude_bin).exists():
            raise RuntimeError(f"claude CLI not found: {self.claude_bin}")
        self._cwd = os.path.realpath(str(workspace))
        argv = [
            self.claude_bin, "--output-format", "stream-json", "--verbose",
            "--model", self.model, "--permission-mode", "acceptEdits",
            "--allowedTools", *self.allowed_tools,
            "--setting-sources", "local", "--strict-mcp-config",
            "--system-prompt-snapshot", "off",
            "--max-budget-usd", str(self.max_budget_usd),
        ]
        if harness.context.text:
            argv += ["--append-system-prompt", harness.context.text]
        state = resume.agent_state if resume is not None else {}
        if state.get("session_id"):
            src = session_dir_for(state["cwd"], self.projects_dir) / f"{state['session_id']}.jsonl"
            self._session_id = str(uuid.uuid4())
            fork_session(src, cut_after_tool_results=int(state["tool_results"]),
                         src_cwd=state["cwd"], dst_cwd=self._cwd,
                         new_session_id=self._session_id, projects_dir=self.projects_dir)
            self._tool_results = int(state["tool_results"])
            argv += ["--resume", self._session_id, "-p", CONTINUE_PROMPT]
        else:
            self._session_id = str(uuid.uuid4())
            self._tool_results = 0
            prompt = (
                f"{task.instruction}\n\nWork only inside the current directory. "
                "When you are done, stop."
            )
            argv += ["--session-id", self._session_id, "-p", prompt]
        self._argv = argv
        self._proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self._cwd, env=agent_env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        return self._session_id

    async def inject_context(self, context: str) -> None:
        # Context is fixed per process (system prompt). Mid-run injection is
        # not supported by -p mode; the wrap hooks inject per prompt instead.
        raise NotImplementedError("ClaudeCodeAdapter injects context at start_task")

    async def terminate(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()

    def _state(self) -> dict[str, Any]:
        return {"session_id": self._session_id, "tool_results": self._tool_results,
                "cwd": self._cwd, "model": self.model}

    async def stream_events(self) -> AsyncIterator[AgentEvent]:
        assert self._proc is not None and self._proc.stdout is not None
        pending: dict[str, tuple[str, dict[str, Any]]] = {}
        saw_result = False
        async for raw in self._proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "system" and event.get("subtype") == "init":
                self._session_id = event.get("session_id") or self._session_id
                yield AgentEvent(kind="init", model_version=event.get("model"))
            elif etype == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        pending[block.get("id", "")] = (block.get("name", ""), block.get("input") or {})
            elif etype == "user":
                for block in (event.get("message") or {}).get("content") or []:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    name, tool_input = pending.pop(block.get("tool_use_id", ""), ("unknown", {}))
                    body = _text_of(block.get("content"))
                    tool, normalized, is_error = normalize_tool_event(
                        name, tool_input, body, Path(self._cwd)
                    )
                    self._tool_results += 1
                    # Only a step that closes its batch of (parallel) tool
                    # calls is a fork point; mid-batch steps carry no resume
                    # state.
                    yield AgentEvent(
                        kind="tool_call", tool=tool, input=normalized, output=body[:4000],
                        is_error=bool(block.get("is_error")) or is_error,
                        agent_state=self._state() if not pending else None,
                    )
            elif etype == "result":
                saw_result = True
                yield AgentEvent(kind="usage", tokens=usage_tokens(event.get("usage")))
                subtype = str(event.get("subtype", ""))
                if subtype.startswith("error_max"):
                    yield AgentEvent(kind="budget_exhausted", output=subtype)
                elif event.get("is_error"):
                    yield AgentEvent(kind="error", output=str(event.get("result"))[:500],
                                     is_error=True)
                else:
                    yield AgentEvent(kind="tool_call", tool=TOOL_COMPLETE, input={},
                                     output=str(event.get("result", ""))[:2000],
                                     agent_state=self._state())
        await self._proc.wait()
        if not saw_result:
            stderr = b""
            if self._proc.stderr is not None:
                stderr = await self._proc.stderr.read()
            yield AgentEvent(kind="error", is_error=True,
                             output=f"claude exited {self._proc.returncode} without a result: "
                                    f"{stderr.decode(errors='replace')[-500:]}")
