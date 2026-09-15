"""Refinement policy — when to reflect (DESIGN §2).

Cheap, non-LLM, runs after every task:

```python
def should_reflect(stats: HarnessStats) -> bool:
    if stats.tasks_since_last_reflection >= 20:        return True
    if stats.repeated_failure_count >= 3:              return True
    if stats.success_rate_drop >= NOISE_FLOOR * 2:     return True
    if stats.tool_cost_increase >= 0.40:               return True
    return False
```

``NOISE_FLOOR`` comes from config. When it has not been measured, the
success-drop trigger is **skipped**, not defaulted — a threshold below
the noise floor fires on randomness.

Regression to the mean: refinement triggers after a bad run and
performance "improves" anyway. That is why the trigger condition is
never the success condition — promotion is decided on holdout tasks
unrelated to the trigger (``gate.py``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.continual.experience import EvaluationRecord, ExperienceStore, TrajectoryRecord
from app.refinement.reflection import signature_patterns

TASKS_BETWEEN_REFLECTIONS = 20
REPEATED_FAILURE_THRESHOLD = 3
TOOL_COST_INCREASE_THRESHOLD = 0.40
RECENT_WINDOW = 10


WORK_RUN_PREFIX = "hx-work-"  # observed tasks run through the runtime


def is_observed_work(trajectory: TrajectoryRecord) -> bool:
    """Observed work, not an experiment: no fork parent, and either run
    as a work task (``hx-work-…``) or recorded outside the evaluation
    runtime (e.g. ``harness wrap``). Comparison runs are excluded — the
    trigger must never be computed from the evaluation itself."""
    if trajectory.fork_parent is not None:
        return False
    run = trajectory.branch_run_id
    return run.startswith(WORK_RUN_PREFIX) or not run.startswith("hx-")


@dataclass
class HarnessStats:
    tasks_since_last_reflection: int
    repeated_failure_count: int
    repeated_failure_signature: str | None
    success_rate_drop: float
    tool_cost_increase: float
    recent_n: int
    baseline_n: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyDecision:
    reflect: bool
    reason: str
    stats: HarnessStats


def _success(ev: EvaluationRecord) -> float:
    return 1.0 if ev.success and not ev.voided else 0.0


async def compute_stats(
    xp: ExperienceStore,
    *,
    harness_id: str,
    agent: str,
    last_reflection_at: float | None,
    tasks: dict[str, Any] | None = None,
    window: int = RECENT_WINDOW,
) -> HarnessStats:
    """Stats over *primary* task trajectories (forks and evaluation runs
    are excluded: they are experiments, not observed work)."""
    rows: list[tuple[TrajectoryRecord, EvaluationRecord]] = [
        (t, e)
        for t, e in await xp.evaluated_trajectories(harness_id=harness_id, agent=agent)
        if is_observed_work(t)
    ]
    since = [
        (t, e) for t, e in rows
        if last_reflection_at is None or t.created_at > last_reflection_at
    ]
    recent = rows[-window:]
    baseline = rows[:-window]

    signature_counts: dict[str, int] = {}
    for t, e in since:
        if _success(e):
            continue
        steps = await xp.full_steps(t.id)
        task = (tasks or {}).get(t.task_id)
        for name in signature_patterns(steps, task):
            signature_counts[name] = signature_counts.get(name, 0) + 1
    top = max(signature_counts.items(), key=lambda kv: kv[1], default=(None, 0))

    def rate(pairs):
        return sum(_success(e) for _, e in pairs) / len(pairs) if pairs else 0.0

    def mean_calls(pairs):
        return sum((e.tool_calls or 0) for _, e in pairs) / len(pairs) if pairs else 0.0

    success_drop = rate(baseline) - rate(recent) if baseline and recent else 0.0
    base_calls = mean_calls(baseline)
    cost_increase = (
        (mean_calls(recent) - base_calls) / base_calls if baseline and base_calls else 0.0
    )
    return HarnessStats(
        tasks_since_last_reflection=len(since),
        repeated_failure_count=top[1],
        repeated_failure_signature=top[0],
        success_rate_drop=success_drop,
        tool_cost_increase=cost_increase,
        recent_n=len(recent),
        baseline_n=len(baseline),
    )


def should_reflect(stats: HarnessStats, *, noise_floor_success: float | None) -> PolicyDecision:
    if stats.tasks_since_last_reflection >= TASKS_BETWEEN_REFLECTIONS:
        return PolicyDecision(True, f"{stats.tasks_since_last_reflection} tasks since last reflection", stats)
    if stats.repeated_failure_count >= REPEATED_FAILURE_THRESHOLD:
        return PolicyDecision(
            True,
            f"repeated failure '{stats.repeated_failure_signature}' "
            f"×{stats.repeated_failure_count}",
            stats,
        )
    if noise_floor_success is not None and stats.success_rate_drop >= 2 * noise_floor_success:
        return PolicyDecision(
            True,
            f"success rate dropped {stats.success_rate_drop:.1%} "
            f"(≥ 2 × noise floor {noise_floor_success:.1%})",
            stats,
        )
    if stats.tool_cost_increase >= TOOL_COST_INCREASE_THRESHOLD:
        return PolicyDecision(True, f"tool cost up {stats.tool_cost_increase:.0%}", stats)
    return PolicyDecision(False, "no evidence", stats)
