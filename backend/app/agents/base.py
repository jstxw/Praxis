"""Agent adapter contract (ARCHITECTURE §8).

```python
class CodingAgentAdapter:
    async def start_task(self, task, workspace, harness) -> str: ...
    async def stream_events(self): ...
    async def inject_context(self, context): ...
    async def terminate(self): ...
```

extended with the one capability the refinement loop needs:
``start_task(..., resume=ResumePoint)`` continues a trajectory from a
checkpoint (workspace snapshot + adapter state) under a possibly
different harness. An adapter that cannot do this sets
``supports_fork = False`` and its candidates fall back to from-scratch
evaluation — reported as such, never silently.

Model-agnosticism is deferred, not claimed: a stdout-only wrapper cannot
produce the per-tool-call events reflection requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from app.continual.harness_object import HarnessSnapshot, MemoryEntry, RenderedContext

# Tool names in trajectories use the frozen six-tool vocabulary
# (INTERFACES.md §3) so reflection sees one language regardless of agent.
TOOL_READ = "read_file"
TOOL_WRITE = "write_file"
TOOL_PATCH = "apply_patch"
TOOL_BASH = "run_bash"
TOOL_GREP = "grep_search"
TOOL_COMPLETE = "task_complete"
FIXED_TOOLS = (TOOL_READ, TOOL_PATCH, TOOL_WRITE, TOOL_BASH, TOOL_GREP, TOOL_COMPLETE)


@dataclass(frozen=True)
class TaskSpec:
    """What the agent is asked to do. The corpus directory (gold fix,
    pristine tests) is trusted-plane data; adapters receive only what
    they are allowed to see (``instruction`` and a workspace copy)."""

    id: str
    instruction: str
    test_command: str
    full_test_command: str
    scope_files: tuple[str, ...]
    test_files: tuple[str, ...]
    capability: str
    difficulty: str
    source_dir: Path
    base_commit: str = "corpus"

    @property
    def workspace_dir(self) -> Path:
        return self.source_dir / "workspace"

    @property
    def gold_dir(self) -> Path:
        return self.source_dir / "gold"


@dataclass
class HarnessBinding:
    """A harness version bound to a pinned memory version for one run."""

    harness_id: str
    memory_version: str
    snapshot: HarnessSnapshot
    memory: list[MemoryEntry]
    context: RenderedContext


@dataclass
class AgentEvent:
    """One observable agent action.

    ``kind="tool_call"`` events become trajectory steps (and checkpoints).
    ``agent_state`` is the adapter's opaque resume token *after* this
    event; ``tokens`` is the adapter's best token attribution for it.
    """

    kind: str  # init | tool_call | message | usage | error
    tool: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    is_error: bool = False
    tokens: int = 0
    agent_state: dict[str, Any] | None = None
    model_version: str | None = None


@dataclass
class ResumePoint:
    """Fork input: continue after ``step`` from this checkpoint."""

    trajectory_id: str
    step: int
    snapshot_id: str
    agent_state: dict[str, Any]
    prefix_tool_calls: int
    prefix_tokens: int


@runtime_checkable
class CodingAgentAdapter(Protocol):
    name: str
    supports_fork: bool

    async def start_task(
        self,
        task: TaskSpec,
        workspace: Path,
        harness: HarnessBinding,
        *,
        resume: ResumePoint | None = None,
        seed: int = 0,
    ) -> str: ...

    def stream_events(self) -> AsyncIterator[AgentEvent]: ...

    async def inject_context(self, context: str) -> None: ...

    async def terminate(self) -> None: ...
