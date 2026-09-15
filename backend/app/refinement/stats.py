"""Pure-Python statistics for paired harness comparison (DESIGN.md §5–§6).

Per-task values are averaged over repetitions within each arm; tests and
intervals then operate on per-task paired deltas. Standard library only, and
deterministic given a seed (a private ``random.Random`` is always used).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

_EXACT_MAX_N = 25


# ---------------------------------------------------------------------------
# Basic moments
# ---------------------------------------------------------------------------


def mean(xs: Sequence[float]) -> float:
    """Arithmetic mean. Raises ValueError on empty input."""
    if len(xs) == 0:
        raise ValueError("mean of empty sequence")
    return math.fsum(xs) / len(xs)


def variance(xs: Sequence[float], ddof: int = 1) -> float:
    """Variance with ``ddof`` delta degrees of freedom; 0.0 when len(xs) <= ddof."""
    n = len(xs)
    if n <= ddof:
        return 0.0
    m = mean(xs)
    return math.fsum((x - m) ** 2 for x in xs) / (n - ddof)


# ---------------------------------------------------------------------------
# Wilcoxon signed-rank
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WilcoxonResult:
    statistic: float  # W+ (sum of positive ranks)
    n_effective: int  # pairs after dropping zero deltas
    p_value: float  # two-sided
    method: str  # "exact" | "normal" | "degenerate"
    rank_biserial: float  # matched-pairs rank-biserial correlation in [-1, 1]


def _average_ranks(values: Sequence[float]) -> tuple[list[float], list[int]]:
    """Ranks (1-based, ties averaged) in input order, plus the sizes of tie groups."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    tie_sizes: list[int] = []
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j + 2) / 2.0  # mean of 1-based ranks i+1 .. j+1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        tie_sizes.append(j - i + 1)
        i = j + 1
    return ranks, tie_sizes


def _exact_distribution(n: int) -> list[int]:
    """counts[s] = number of subsets of {1..n} whose sum is s (s = 0..n(n+1)/2)."""
    total = n * (n + 1) // 2
    counts = [0] * (total + 1)
    counts[0] = 1
    for r in range(1, n + 1):
        for s in range(total, r - 1, -1):
            counts[s] += counts[s - r]
    return counts


def _exact_p_value(w_plus: int, n: int) -> float:
    """Exact two-sided p for integer W+ with n untied, nonzero pairs."""
    counts = _exact_distribution(n)
    denom = 2**n
    lower = sum(counts[: w_plus + 1])
    upper = sum(counts[w_plus:])
    return min(1.0, 2.0 * min(lower, upper) / denom)


def _normal_p_value(w_plus: float, n: int, tie_sizes: Sequence[int] = ()) -> float | None:
    """Two-sided normal-approximation p with tie and continuity correction.

    Returns None when the variance is zero (degenerate).
    """
    mu = n * (n + 1) / 4.0
    var = n * (n + 1) * (2 * n + 1) / 24.0 - math.fsum(t**3 - t for t in tie_sizes) / 48.0
    if var <= 0.0:
        return None
    sigma = math.sqrt(var)
    z = max(0.0, abs(w_plus - mu) - 0.5) / sigma
    return min(1.0, math.erfc(z / math.sqrt(2.0)))


def wilcoxon_signed_rank(deltas: Sequence[float], *, zero_tol: float = 1e-12) -> WilcoxonResult:
    """Two-sided Wilcoxon signed-rank test on paired deltas."""
    nonzero = [d for d in deltas if abs(d) > zero_tol]
    n = len(nonzero)
    if n == 0:
        return WilcoxonResult(0.0, 0, 1.0, "degenerate", 0.0)

    ranks, tie_sizes = _average_ranks([abs(d) for d in nonzero])
    w_plus = math.fsum(r for r, d in zip(ranks, nonzero) if d > 0)
    w_minus = math.fsum(r for r, d in zip(ranks, nonzero) if d < 0)
    denom = w_plus + w_minus
    rank_biserial = (w_plus - w_minus) / denom if denom > 0 else 0.0
    has_ties = any(t > 1 for t in tie_sizes)

    if n <= _EXACT_MAX_N and not has_ties:
        p = _exact_p_value(int(round(w_plus)), n)
        return WilcoxonResult(w_plus, n, p, "exact", rank_biserial)

    p_normal = _normal_p_value(w_plus, n, tie_sizes)
    if p_normal is None:
        return WilcoxonResult(w_plus, n, 1.0, "degenerate", rank_biserial)
    return WilcoxonResult(w_plus, n, p_normal, "normal", rank_biserial)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapCI:
    estimate: float  # statistic on the original sample
    lower: float
    upper: float
    level: float  # e.g. 0.95
    n_resamples: int


def bootstrap_ci(
    xs: Sequence[float],
    *,
    statistic: Callable[[Sequence[float]], float] = mean,
    level: float = 0.95,
    n_resamples: int = 10_000,
    seed: int = 0,
) -> BootstrapCI:
    """Percentile bootstrap confidence interval; identical output for identical seed."""
    if len(xs) == 0:
        raise ValueError("bootstrap_ci requires a non-empty sample")
    if not 0.0 < level < 1.0:
        raise ValueError("level must be in (0, 1)")
    if n_resamples < 1:
        raise ValueError("n_resamples must be >= 1")

    data = list(xs)
    estimate = statistic(data)
    if len(data) == 1:
        return BootstrapCI(estimate, estimate, estimate, level, n_resamples)

    rng = random.Random(seed)
    n = len(data)
    stats = sorted(statistic(rng.choices(data, k=n)) for _ in range(n_resamples))
    last = n_resamples - 1
    lo_idx = min(last, max(0, math.floor((1.0 - level) / 2.0 * last)))
    hi_idx = min(last, max(0, math.ceil((1.0 + level) / 2.0 * last)))
    return BootstrapCI(estimate, stats[lo_idx], stats[hi_idx], level, n_resamples)


# ---------------------------------------------------------------------------
# Multiple comparisons
# ---------------------------------------------------------------------------


def holm_bonferroni(p_values: Sequence[float], alpha: float = 0.05) -> list[tuple[float, bool]]:
    """Holm step-down adjusted p-values and reject flags, in the original order."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[idx]))
        adjusted[idx] = running
    return [(p, p <= alpha) for p in adjusted]


def bonferroni_level(level: float, m: int) -> float:
    """Per-comparison confidence level for m simultaneous intervals."""
    if m < 1:
        raise ValueError("m must be >= 1")
    return 1.0 - (1.0 - level) / m


# ---------------------------------------------------------------------------
# Task selection and paired comparison
# ---------------------------------------------------------------------------


def discrimination(pass_fractions: Sequence[float]) -> float:
    """Disc(t) = Var_h[V(pi_h, t)]: population variance across candidates (ρ(1−ρ) if binary)."""
    if len(pass_fractions) == 0:
        return 0.0
    return variance(pass_fractions, ddof=0)


def paired_task_deltas(
    parent: Mapping[str, Sequence[float]],
    child: Mapping[str, Sequence[float]],
    *,
    relative: bool = False,
    eps: float = 1e-9,
) -> dict[str, float]:
    """Per-task (child − parent) deltas of repetition means, over shared task ids."""
    shared = sorted(set(parent) & set(child))
    if not shared:
        raise ValueError("parent and child share no task ids")
    out: dict[str, float] = {}
    for task in shared:
        if len(parent[task]) == 0 or len(child[task]) == 0:
            raise ValueError(f"task {task!r} has no repetitions in one arm")
        p_mean = mean(parent[task])
        c_mean = mean(child[task])
        diff = c_mean - p_mean
        out[task] = diff / max(abs(p_mean), eps) if relative else diff
    return out


@dataclass(frozen=True)
class PairedComparison:
    metric: str
    n_tasks: int
    n_reps: int  # minimum reps per task across both arms
    delta: float  # mean of per-task deltas
    ci: BootstrapCI
    wilcoxon: WilcoxonResult


def compare_paired(
    metric: str,
    parent: Mapping[str, Sequence[float]],
    child: Mapping[str, Sequence[float]],
    *,
    relative: bool = False,
    level: float = 0.95,
    n_resamples: int = 10_000,
    seed: int = 0,
) -> PairedComparison:
    """Paired comparison: bootstrap CI of the mean delta plus Wilcoxon signed-rank."""
    deltas = paired_task_deltas(parent, child, relative=relative)
    values = list(deltas.values())
    n_reps = min(min(len(parent[t]), len(child[t])) for t in deltas)
    ci = bootstrap_ci(values, level=level, n_resamples=n_resamples, seed=seed)
    return PairedComparison(
        metric=metric,
        n_tasks=len(values),
        n_reps=n_reps,
        delta=ci.estimate,
        ci=ci,
        wilcoxon=wilcoxon_signed_rank(values),
    )


def variance_decomposition(groups: Mapping[str, Sequence[float]]) -> dict[str, float]:
    """Split outcome variance into shared-prefix (between) and tail (within) parts."""
    pooled = [x for vals in groups.values() for x in vals]
    if not pooled:
        return {"total": 0.0, "within": 0.0, "between": 0.0, "within_fraction": 0.0}
    total = variance(pooled, ddof=1)
    within_vars = [variance(vals, ddof=1) for vals in groups.values() if len(vals) >= 2]
    within = mean(within_vars) if within_vars else 0.0
    group_means = [mean(vals) for vals in groups.values() if len(vals) >= 1]
    between = variance(group_means, ddof=1)
    return {
        "total": total,
        "within": within,
        "between": between,
        "within_fraction": within / total if total != 0 else 0.0,
    }
