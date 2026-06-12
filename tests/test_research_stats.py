"""Sanity tests for the research statistics primitives."""
from __future__ import annotations

import random

from pm.research import stats


def test_summarise_handles_all_zero():
    s = stats.summarise([0.0, 0.0, 0.0])
    assert s["n_lab"] == 3
    assert s["n_moved"] == 0
    assert s["h_raw"] == 0.0
    assert s["h_cond"] is None
    assert s["mean"] == 0.0


def test_summarise_basic():
    s = stats.summarise([0.0, 0.01, -0.01, 0.005, 0.0, 0.0])
    assert s["n_lab"] == 6
    assert s["n_moved"] == 3
    assert abs(s["h_raw"] - 2 / 6) < 1e-9
    assert abs(s["h_cond"] - 2 / 3) < 1e-9


def test_sign_test_known_values():
    assert abs(stats.sign_test(5, 10, alternative="two-sided") - 1.0) < 1e-9
    # 8 of 10, two-sided: 2 * P(X >= 8) under p=0.5
    assert 0.10 < stats.sign_test(8, 10, alternative="two-sided") < 0.12
    # 75 of 131, one-sided greater
    p_greater = stats.sign_test(75, 131, alternative="greater")
    p_less = stats.sign_test(75, 131, alternative="less")
    assert p_greater < 0.10
    assert p_less > 0.90


def test_sign_test_empty():
    assert stats.sign_test(0, 0) != stats.sign_test(0, 0)  # NaN


def test_clopper_pearson_brackets_estimate():
    lo, hi = stats.clopper_pearson(50, 100)
    assert lo < 0.5 < hi
    # narrower for larger n
    lo2, hi2 = stats.clopper_pearson(500, 1000)
    assert (hi2 - lo2) < (hi - lo)


def test_clopper_pearson_edges():
    lo, hi = stats.clopper_pearson(0, 10)
    assert lo == 0.0 and hi < 1.0
    lo, hi = stats.clopper_pearson(10, 10)
    assert lo > 0.0 and hi == 1.0


def test_bootstrap_ci_is_deterministic_with_seed():
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    sample = [0.001, 0.002, -0.001, 0.0, 0.0, 0.003]
    ci1 = stats.bootstrap_ci(sample, lambda xs: sum(xs) / len(xs),
                             n_boot=500, rng=rng1)
    ci2 = stats.bootstrap_ci(sample, lambda xs: sum(xs) / len(xs),
                             n_boot=500, rng=rng2)
    assert ci1 == ci2


def test_bootstrap_ci_contains_mean():
    rng = random.Random(42)
    sample = [0.001] * 50 + [0.002] * 50
    expected_mean = sum(sample) / len(sample)
    lo, hi = stats.bootstrap_ci(sample, lambda xs: sum(xs) / len(xs),
                                n_boot=2000, rng=rng)
    assert lo <= expected_mean <= hi


def test_benjamini_hochberg_one_clear_signal():
    pvals = [0.001, 0.5, 0.7, 0.8, 0.9]
    reject = stats.benjamini_hochberg(pvals, q=0.05)
    assert reject == [True, False, False, False, False]


def test_benjamini_hochberg_no_signal():
    pvals = [0.5, 0.6, 0.7, 0.8, 0.9]
    assert stats.benjamini_hochberg(pvals, q=0.05) == [False] * 5


def test_bh_adjusted_monotone_in_p():
    pvals = [0.001, 0.04, 0.03, 0.20, 0.50]
    adj = stats.benjamini_hochberg_adjusted(pvals)
    sorted_pairs = sorted(zip(pvals, adj))
    for i in range(1, len(sorted_pairs)):
        assert sorted_pairs[i][1] >= sorted_pairs[i - 1][1] - 1e-9


def test_wilcoxon_runs_on_realistic_sample():
    p = stats.wilcoxon_signed_rank([0.001, 0.002, -0.001, 0.005, 0.003, 0.0, 0.0, 0.008])
    assert 0.0 < p < 1.0


def test_mann_whitney_separates_distributions():
    a = [0.01, 0.02, 0.015, 0.018, 0.012, 0.020, 0.014, 0.019]
    b = [-0.01, -0.02, -0.015, -0.018, -0.012, -0.020, -0.014, -0.019]
    p = stats.mann_whitney_u(a, b)
    assert p < 0.05


def test_mann_whitney_no_difference():
    a = [0.01, -0.01, 0.005, -0.005] * 5
    b = [0.01, -0.01, 0.005, -0.005] * 5
    p = stats.mann_whitney_u(a, b)
    assert p > 0.5
