"""Tests for app.refinement.stats."""

from __future__ import annotations

import itertools
import random
from collections import Counter

import pytest

from app.refinement.stats import (
    BootstrapCI,
    bonferroni_level,
    bootstrap_ci,
    compare_paired,
    discrimination,
    holm_bonferroni,
    mean,
    paired_task_deltas,
    variance,
    variance_decomposition,
    wilcoxon_signed_rank,
)
from app.refinement.stats import _exact_distribution, _normal_p_value


# --- moments ---------------------------------------------------------------


def test_mean_and_variance_basic():
    assert mean([1.0, 2.0, 3.0, 4.0]) == pytest.approx(2.5)
    assert variance([1.0, 2.0, 3.0, 4.0]) == pytest.approx(5.0 / 3.0)
    assert variance([1.0, 2.0, 3.0, 4.0], ddof=0) == pytest.approx(1.25)
    assert variance([7.0]) == 0.0
    assert variance([], ddof=0) == 0.0


# --- Wilcoxon --------------------------------------------------------------


def test_wilcoxon_all_positive_n5_exact():
    r = wilcoxon_signed_rank([0.5, 1.2, 2.0, 3.3, 4.1])
    assert r.method == "exact"
    assert r.n_effective == 5
    assert r.statistic == 15
    assert r.p_value == pytest.approx(2 / 32)
    assert r.rank_biserial == pytest.approx(1.0)


def test_wilcoxon_all_positive_n6_exact():
    r = wilcoxon_signed_rank([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    assert r.method == "exact"
    assert r.statistic == 21
    assert r.p_value == pytest.approx(2 / 64)


def test_wilcoxon_all_negative_is_symmetric_to_positive():
    r = wilcoxon_signed_rank([-1.0, -2.0, -3.0, -4.0, -5.0])
    assert r.statistic == 0
    assert r.p_value == pytest.approx(2 / 32)
    assert r.rank_biserial == pytest.approx(-1.0)


def test_wilcoxon_symmetric_sample_p_one():
    # ranks 1..4 total 10; positives at ranks {1, 4} → W+ = W− = 5
    r = wilcoxon_signed_rank([1.0, -2.0, -3.0, 4.0])
    assert r.method == "exact"
    assert r.statistic == 5
    assert r.p_value == pytest.approx(1.0)
    assert r.rank_biserial == 0.0


def test_exact_distribution_matches_brute_force():
    for n in range(1, 11):
        brute = Counter()
        for signs in itertools.product((0, 1), repeat=n):
            brute[sum(rank for rank, s in zip(range(1, n + 1), signs) if s)] += 1
        counts = _exact_distribution(n)
        assert sum(counts) == 2**n
        assert len(counts) == n * (n + 1) // 2 + 1
        for s, c in enumerate(counts):
            assert c == brute[s], (n, s)


def test_exact_p_matches_brute_force_enumeration():
    rng = random.Random(123)
    for n in range(1, 11):
        signs = [rng.choice((-1, 1)) for _ in range(n)]
        deltas = [s * (i + 1) * 0.7 for i, s in enumerate(signs)]
        r = wilcoxon_signed_rank(deltas)
        w = r.statistic
        all_w = [
            sum(rank for rank, s in zip(range(1, n + 1), assign) if s)
            for assign in itertools.product((0, 1), repeat=n)
        ]
        lower = sum(1 for x in all_w if x <= w) / 2**n
        upper = sum(1 for x in all_w if x >= w) / 2**n
        assert r.p_value == pytest.approx(min(1.0, 2 * min(lower, upper)))


def test_wilcoxon_drops_zeros():
    r = wilcoxon_signed_rank([0.0, 1e-15, 1.0, 2.0, 3.0, 4.0, 5.0])
    assert r.n_effective == 5
    assert r.statistic == 15
    assert r.p_value == pytest.approx(2 / 32)


def test_wilcoxon_ties_force_normal():
    r = wilcoxon_signed_rank([1.0, 1.0, 2.0, -3.0, 4.0])
    assert r.method == "normal"
    # |d| ranks: 1.5, 1.5, 3, 4, 5 → W+ = 1.5 + 1.5 + 3 + 5
    assert r.statistic == pytest.approx(11.0)
    assert 0.0 < r.p_value <= 1.0


def test_wilcoxon_normal_symmetric_ties_p_one():
    r = wilcoxon_signed_rank([1.0, -1.0, 2.0, -2.0])
    assert r.method == "normal"
    assert r.p_value == pytest.approx(1.0)
    assert r.rank_biserial == 0.0


def test_wilcoxon_degenerate_all_zero():
    for deltas in ([], [0.0, 0.0, 0.0]):
        r = wilcoxon_signed_rank(deltas)
        assert r.method == "degenerate"
        assert r.n_effective == 0
        assert r.p_value == 1.0
        assert r.statistic == 0
        assert r.rank_biserial == 0


def test_wilcoxon_large_n_uses_normal():
    r = wilcoxon_signed_rank([float(i + 1) for i in range(30)])
    assert r.method == "normal"
    assert r.p_value < 1e-4


def test_normal_approx_close_to_exact_n20():
    for seed in range(10):
        rng = random.Random(seed)
        deltas = [rng.choice((-1, 1)) * (i + 1) * 0.3 for i in range(20)]
        exact = wilcoxon_signed_rank(deltas)
        assert exact.method == "exact"
        approx = _normal_p_value(exact.statistic, 20)
        assert approx is not None
        assert abs(approx - exact.p_value) < 0.02, (seed, exact.p_value, approx)


# --- bootstrap -------------------------------------------------------------


def test_bootstrap_deterministic_for_seed():
    xs = [0.1, -0.3, 0.5, 0.2, 0.9, -0.1, 0.4]
    a = bootstrap_ci(xs, n_resamples=2000, seed=7)
    b = bootstrap_ci(xs, n_resamples=2000, seed=7)
    assert a == b
    assert isinstance(a, BootstrapCI)
    c = bootstrap_ci(xs, n_resamples=2000, seed=8)
    assert (c.lower, c.upper) != (a.lower, a.upper)


def test_bootstrap_ci_contains_estimate():
    rng = random.Random(1)
    xs = [rng.gauss(1.0, 2.0) for _ in range(15)]
    ci = bootstrap_ci(xs, n_resamples=3000, seed=0)
    assert ci.estimate == pytest.approx(mean(xs))
    assert ci.lower <= ci.estimate <= ci.upper
    assert ci.lower < ci.upper
    assert ci.level == 0.95
    assert ci.n_resamples == 3000


def test_bootstrap_constant_sample_zero_width():
    ci = bootstrap_ci([2.5] * 8, n_resamples=500)
    assert ci.lower == ci.upper
    assert ci.lower == pytest.approx(2.5)


def test_bootstrap_single_element():
    ci = bootstrap_ci([3.0])
    assert ci.lower == ci.upper == ci.estimate == 3.0


def test_bootstrap_empty_raises():
    with pytest.raises(ValueError):
        bootstrap_ci([])


def test_bootstrap_custom_statistic():
    xs = [1.0, 2.0, 3.0, 100.0]
    ci = bootstrap_ci(xs, statistic=max, n_resamples=500, seed=3)
    assert ci.estimate == 100.0
    assert ci.upper == 100.0


# --- multiple comparisons --------------------------------------------------


def test_holm_textbook_example():
    result = holm_bonferroni([0.01, 0.04, 0.03, 0.005], alpha=0.05)
    adjusted = [p for p, _ in result]
    rejects = [r for _, r in result]
    assert adjusted == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert rejects == [True, False, False, True]


def test_holm_caps_at_one_and_empty():
    result = holm_bonferroni([0.5, 0.9])
    assert [p for p, _ in result] == pytest.approx([1.0, 1.0])
    assert holm_bonferroni([]) == []


def test_bonferroni_level():
    assert bonferroni_level(0.95, 1) == pytest.approx(0.95)
    assert bonferroni_level(0.95, 3) == pytest.approx(1 - 0.05 / 3)
    with pytest.raises(ValueError):
        bonferroni_level(0.95, 0)


# --- discrimination --------------------------------------------------------


def test_discrimination_binary_matches_rho():
    for outcomes in ([1, 0, 0, 0], [1, 1, 0, 0], [1, 1, 1, 0, 0], [1, 0, 0, 0, 0, 0, 0, 0, 0, 0]):
        rho = sum(outcomes) / len(outcomes)
        assert discrimination([float(o) for o in outcomes]) == pytest.approx(rho * (1 - rho))


def test_discrimination_dead_zones():
    assert discrimination([1.0] * 6) == 0.0
    assert discrimination([0.0] * 6) == 0.0
    assert discrimination([]) == 0.0


# --- paired deltas ---------------------------------------------------------


def test_paired_task_deltas_intersection_and_averaging():
    parent = {"b": [10.0, 12.0], "a": [4.0, 6.0, 5.0], "only_parent": [1.0]}
    child = {"a": [3.0, 3.0], "b": [9.0], "only_child": [2.0]}
    deltas = paired_task_deltas(parent, child)
    assert list(deltas) == ["a", "b"]
    assert deltas["a"] == pytest.approx(3.0 - 5.0)
    assert deltas["b"] == pytest.approx(9.0 - 11.0)


def test_paired_task_deltas_relative():
    parent = {"a": [10.0, 10.0], "z": [0.0]}
    child = {"a": [8.0, 7.0], "z": [1e-9]}
    deltas = paired_task_deltas(parent, child, relative=True)
    assert deltas["a"] == pytest.approx(-0.25)
    assert deltas["z"] == pytest.approx(1.0)  # divides by eps


def test_paired_task_deltas_empty_intersection_raises():
    with pytest.raises(ValueError):
        paired_task_deltas({"a": [1.0]}, {"b": [1.0]})


def test_compare_paired_end_to_end():
    parent = {f"t{i}": [20.0 + i, 22.0 + i, 21.0 + i] for i in range(8)}
    child = {f"t{i}": [15.0 + i * 1.1, 16.0 + i] for i in range(8)}
    child["extra"] = [1.0]
    res = compare_paired("tool_calls", parent, child, n_resamples=2000, seed=4)
    assert res.metric == "tool_calls"
    assert res.n_tasks == 8
    assert res.n_reps == 2
    expected = mean(list(paired_task_deltas(parent, child).values()))
    assert res.delta == pytest.approx(expected)
    assert res.ci.upper < 0
    assert res.wilcoxon.method == "exact"
    assert res.wilcoxon.statistic == 0
    assert res.wilcoxon.p_value == pytest.approx(2 / 256)
    assert res == compare_paired("tool_calls", parent, child, n_resamples=2000, seed=4)


# --- variance decomposition -----------------------------------------------


def test_variance_decomposition_identical_within_groups():
    out = variance_decomposition({"p1": [1.0, 1.0, 1.0], "p2": [3.0, 3.0, 3.0]})
    assert out["within"] == 0.0
    assert out["within_fraction"] == 0.0
    assert out["between"] == pytest.approx(2.0)  # var([1, 3]) ddof=1
    assert out["total"] == pytest.approx(1.2)  # var([1,1,1,3,3,3]) = 6/5


def test_variance_decomposition_hand_checked():
    # g1 = [0, 2]: mean 1, var 2; g2 = [4, 6]: mean 5, var 2; g3 = [9]: mean 9
    out = variance_decomposition({"g1": [0.0, 2.0], "g2": [4.0, 6.0], "g3": [9.0]})
    pooled = [0.0, 2.0, 4.0, 6.0, 9.0]  # mean 4.2, SS = 17.64+4.84+0.04+3.24+23.04 = 48.8
    assert out["total"] == pytest.approx(48.8 / 4)
    assert out["total"] == pytest.approx(variance(pooled))
    assert out["within"] == pytest.approx(2.0)
    assert out["between"] == pytest.approx(16.0)  # var([1, 5, 9]) ddof=1
    assert out["within_fraction"] == pytest.approx(2.0 / 12.2)


def test_variance_decomposition_empty():
    zeros = {"total": 0.0, "within": 0.0, "between": 0.0, "within_fraction": 0.0}
    assert variance_decomposition({}) == zeros
    assert variance_decomposition({"a": []}) == zeros
