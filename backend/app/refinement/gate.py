"""Compare to parent, and the promotion gate (DESIGN §6).

Metric tiers:

| tier | metric | role |
|---|---|---|
| primary    | tool calls, tokens, redundant reads | continuous; where wins are detectable |
| guard      | task success                        | must not decrease |
| quality    | diff size, out-of-scope files, regressions | "passed, wrecked the code" |
| complexity | harness bytes, skills, injected tokens | penalty term |

Every metric is reported as an **improvement** (positive = better):
efficiency metrics as the relative reduction ``(parent − child)/parent``
per task, success as the absolute per-task rate difference. Statistics
are on per-task paired deltas: bootstrap CI (Bonferroni-widened for the
number of candidates) and Wilcoxon signed-rank with Holm correction
across candidates. Decisions use **CI lower bounds**, never point
estimates.

The gate, with its two noise-aware readings stated openly:

```python
if candidate.holdout_tasks < cfg.MIN_TASKS:      reject
if candidate.reps_per_task < cfg.MIN_REPS:       reject
if candidate.tests_modified:                     reject   # audit
if candidate.success_ci_lower < parent.success:  reject   # (1)
if candidate.regression_delta < 0:               reject   # (2)
if candidate.cost_delta > cfg.MAX_COST:          reject
if candidate.effect_size < cfg.NOISE_FLOOR * 2:  reject
```

(1) Taken literally — a candidate's success *CI lower bound* against the
parent's *point estimate* — this rejects every candidate whose success
equals its parent's, since any CI at n=10 dips below its own mean. It
would make promotion impossible exactly when the efficiency win is
clean. Implemented as: the *paired* success-improvement CI lower bound
must not fall below ``−NOISE_FLOOR[success]``. "Must not decrease"
beyond what the null test says is noise.

(2) Likewise noise-aware: the regression-set success improvement point
estimate must not fall below ``−NOISE_FLOOR[success]``.

Recorded in ``docs/DECISIONS.md``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.refinement.config import GateSettings, NoiseFloor
from app.refinement.evaluation import (
    LOWER_IS_BETTER,
    RunResult,
    arm_audit,
    arm_task_values,
)
from app.refinement.stats import bonferroni_level, compare_paired, holm_bonferroni

PRIMARY_METRICS = ("tool_calls", "tokens", "redundant_reads")
QUALITY_METRICS = ("diff_size", "files_out_of_scope", "regressions")
ALL_METRICS = PRIMARY_METRICS + ("success",) + QUALITY_METRICS + ("context_tokens",)


@dataclass
class MetricResult:
    metric: str
    improvement: float  # positive = child better
    ci_lower: float
    ci_upper: float
    p_value: float
    n_tasks: int
    n_reps: int
    parent_mean: float
    child_mean: float
    level: float
    p_adjusted: float | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def compare_metric(
    results: list[RunResult],
    parent_arm: str,
    child_arm: str,
    metric: str,
    *,
    level: float = 0.95,
    n_resamples: int = 10_000,
    seed: int = 0,
    task_ids: list[str] | None = None,
) -> MetricResult:
    parent = arm_task_values(results, parent_arm, metric)
    child = arm_task_values(results, child_arm, metric)
    if task_ids is not None:
        parent = {t: v for t, v in parent.items() if t in task_ids}
        child = {t: v for t, v in child.items() if t in task_ids}
    relative = metric != "success"
    comparison = compare_paired(
        metric, parent, child, relative=relative, level=level,
        n_resamples=n_resamples, seed=seed,
    )
    # compare_paired reports child − parent; flip for lower-is-better.
    sign = -1.0 if LOWER_IS_BETTER.get(metric, True) else 1.0
    lo, hi = sign * comparison.ci.lower, sign * comparison.ci.upper
    shared = sorted(set(parent) & set(child))
    parent_mean = sum(sum(parent[t]) / len(parent[t]) for t in shared) / len(shared)
    child_mean = sum(sum(child[t]) / len(child[t]) for t in shared) / len(shared)
    return MetricResult(
        metric=metric,
        improvement=sign * comparison.delta,
        ci_lower=min(lo, hi),
        ci_upper=max(lo, hi),
        p_value=comparison.wilcoxon.p_value,
        n_tasks=comparison.n_tasks,
        n_reps=comparison.n_reps,
        parent_mean=parent_mean,
        child_mean=child_mean,
        level=level,
    )


@dataclass
class CandidateVerdict:
    arm: str
    promotable: bool
    reasons: list[str]
    metrics: dict[str, MetricResult]
    regression: dict[str, MetricResult]
    effect_size: float
    complexity_delta: float
    score: float
    audit: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "promotable": self.promotable,
            "reasons": self.reasons,
            "metrics": {k: v.to_json() for k, v in self.metrics.items()},
            "regression": {k: v.to_json() for k, v in self.regression.items()},
            "effect_size": self.effect_size,
            "complexity_delta": self.complexity_delta,
            "score": self.score,
            "audit": self.audit,
        }


def judge_candidates(
    *,
    holdout: list[RunResult],
    regression: list[RunResult] | None,
    parent_arm: str,
    child_arms: list[str],
    settings: GateSettings,
    noise_floor: NoiseFloor,
    complexity_delta: dict[str, float],
    seed: int = 0,
    n_resamples: int = 10_000,
) -> list[CandidateVerdict]:
    """Apply the gate to every candidate against one parent."""
    m = max(1, len(child_arms))
    level = bonferroni_level(settings.level, m)
    primary = settings.primary_metric
    floor_success = noise_floor.floor("success")
    floor_primary = noise_floor.floor(primary)

    verdicts: list[CandidateVerdict] = []
    for arm in child_arms:
        metrics = {
            metric: compare_metric(holdout, parent_arm, arm, metric, level=level,
                                   n_resamples=n_resamples, seed=seed)
            for metric in ALL_METRICS
        }
        reg_metrics: dict[str, MetricResult] = {}
        if regression:
            reg_metrics["success"] = compare_metric(
                regression, parent_arm, arm, "success", level=level,
                n_resamples=n_resamples, seed=seed,
            )
        verdicts.append(
            CandidateVerdict(
                arm=arm,
                promotable=True,
                reasons=[],
                metrics=metrics,
                regression=reg_metrics,
                effect_size=metrics[primary].ci_lower,
                complexity_delta=complexity_delta.get(arm, 0.0),
                score=metrics[primary].ci_lower
                - settings.lambda_complexity * complexity_delta.get(arm, 0.0),
                audit={
                    "holdout": arm_audit(holdout, arm).to_json(),
                    "regression": arm_audit(regression, arm).to_json() if regression else None,
                },
            )
        )

    # Holm across candidates on the primary metric: three candidates are
    # three chances to get lucky.
    adjusted = holm_bonferroni([v.metrics[primary].p_value for v in verdicts], settings.alpha)
    for verdict, (p_adj, _reject) in zip(verdicts, adjusted):
        verdict.metrics[primary].p_adjusted = p_adj

    for v in verdicts:
        reasons = v.reasons
        pm = v.metrics[primary]
        if pm.n_tasks < settings.min_tasks:
            reasons.append(f"holdout tasks {pm.n_tasks} < MIN_TASKS {settings.min_tasks}")
        if pm.n_reps < settings.min_reps:
            reasons.append(f"reps per task {pm.n_reps} < MIN_REPS {settings.min_reps}")
        audits = [a for a in (v.audit["holdout"], v.audit["regression"]) if a]
        if any(a["tests_modified"] or a["voided"] for a in audits):
            reasons.append("audit: tests modified or run voided")
        success = v.metrics["success"]
        if success.ci_lower < -floor_success:
            reasons.append(
                f"success guard: improvement CI lower {success.ci_lower:+.3f} "
                f"< -noise floor {floor_success:.3f}"
            )
        if "success" in v.regression and v.regression["success"].improvement < -floor_success:
            reasons.append(
                f"regression set: success {v.regression['success'].improvement:+.3f} "
                f"< -noise floor {floor_success:.3f}"
            )
        cost = -v.metrics["tokens"].improvement  # relative token increase
        if cost > settings.max_cost:
            reasons.append(f"cost: tokens +{cost:.1%} > MAX_COST {settings.max_cost:.0%}")
        if v.effect_size < 2 * floor_primary:
            reasons.append(
                f"effect: {primary} CI lower {v.effect_size:+.3f} "
                f"< 2 × noise floor {2 * floor_primary:.3f}"
            )
        if pm.p_adjusted is not None and pm.p_adjusted > settings.alpha:
            reasons.append(
                f"significance: Holm-adjusted p {pm.p_adjusted:.3f} > alpha {settings.alpha}"
            )
        v.promotable = not reasons
    return verdicts


def select_promotion(verdicts: list[CandidateVerdict]) -> CandidateVerdict | None:
    """Among promotable candidates, the best complexity-penalized score —
    prefer the smallest harness producing the improvement."""
    eligible = [v for v in verdicts if v.promotable]
    if not eligible:
        return None
    return max(eligible, key=lambda v: (v.score, -v.complexity_delta))
