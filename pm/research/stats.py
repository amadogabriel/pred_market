"""Statistical primitives for the signal-log analysis.

Stdlib-only. Hand-rolled implementations of:

- Bootstrap CIs (vanilla and block-by-token)
- Sign / exact-binomial tests
- Wilcoxon signed-rank (one-sample, against 0)
- Mann-Whitney U (two-sample, two-sided)
- Benjamini-Hochberg FDR correction
- Clopper-Pearson exact-binomial CI

These are deterministic; all RNG goes through a passed-in `random.Random`
instance (seeded externally) so reruns are bit-exact.

This is *not* a stats library and is not optimised. It is intentionally
auditable: every function is < 30 lines and uses only `math` + `statistics`.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass


# ---------- summaries ----------

@dataclass
class KindSummary:
    kind: str
    n: int             # total emitted signals
    n_lab: int         # labeled
    n_moved: int       # nonzero outcome
    h_raw: float | None
    h_cond: float | None
    mean: float | None
    median: float | None
    q25: float | None
    q75: float | None
    stdev: float | None


def summarise(outcomes: list[float]) -> dict:
    """Outcomes assumed to be labeled (no NULLs). Empty -> all None."""
    n_lab = len(outcomes)
    if n_lab == 0:
        return {"n_lab": 0, "n_moved": 0, "h_raw": None, "h_cond": None,
                "mean": None, "median": None, "q25": None, "q75": None,
                "stdev": None}
    nz = [o for o in outcomes if o != 0]
    n_moved = len(nz)
    pos_all = sum(1 for o in outcomes if o > 0)
    pos_mv = sum(1 for o in nz if o > 0)
    return {
        "n_lab": n_lab,
        "n_moved": n_moved,
        "h_raw": pos_all / n_lab,
        "h_cond": pos_mv / n_moved if n_moved else None,
        "mean": statistics.fmean(outcomes),
        "median": statistics.median(outcomes),
        "q25": _quantile(sorted(outcomes), 0.25),
        "q75": _quantile(sorted(outcomes), 0.75),
        "stdev": statistics.stdev(outcomes) if n_lab > 1 else None,
    }


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interp quantile (type 7, R default). Caller passes sorted list."""
    n = len(sorted_vals)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_vals[0]
    h = (n - 1) * q
    lo = math.floor(h)
    hi = math.ceil(h)
    return sorted_vals[lo] + (h - lo) * (sorted_vals[hi] - sorted_vals[lo])


# ---------- bootstrap CIs ----------

def bootstrap_ci(values: list[float], statfn, *, n_boot: int = 10_000,
                 ci: float = 0.95, rng: random.Random | None = None) -> tuple[float, float]:
    """Percentile bootstrap CI. statfn maps a resample list -> a scalar."""
    if not values:
        return (float("nan"), float("nan"))
    rng = rng or random.Random(42)
    n = len(values)
    samples: list[float] = []
    for _ in range(n_boot):
        rs = [values[rng.randrange(n)] for _ in range(n)]
        try:
            samples.append(statfn(rs))
        except Exception:  # noqa: BLE001
            continue
    if not samples:
        return (float("nan"), float("nan"))
    samples.sort()
    lo = _quantile(samples, (1 - ci) / 2)
    hi = _quantile(samples, 1 - (1 - ci) / 2)
    return (lo, hi)


def block_bootstrap_ci(grouped: dict[str, list[float]], statfn, *,
                       n_boot: int = 10_000, ci: float = 0.95,
                       rng: random.Random | None = None) -> tuple[float, float]:
    """Resample groups (tokens) with replacement, flatten, then apply statfn."""
    if not grouped:
        return (float("nan"), float("nan"))
    rng = rng or random.Random(42)
    keys = list(grouped.keys())
    samples: list[float] = []
    for _ in range(n_boot):
        chosen = [grouped[keys[rng.randrange(len(keys))]] for _ in range(len(keys))]
        flat = [o for group in chosen for o in group]
        if not flat:
            continue
        try:
            samples.append(statfn(flat))
        except Exception:  # noqa: BLE001
            continue
    if not samples:
        return (float("nan"), float("nan"))
    samples.sort()
    lo = _quantile(samples, (1 - ci) / 2)
    hi = _quantile(samples, 1 - (1 - ci) / 2)
    return (lo, hi)


# ---------- hit-rate tests ----------

def clopper_pearson(k: int, n: int, ci: float = 0.95) -> tuple[float, float]:
    """Exact-binomial CI for a proportion. Uses inverse-beta via numerical root."""
    if n == 0:
        return (0.0, 1.0)
    alpha = 1 - ci
    lo = 0.0 if k == 0 else _beta_quantile(alpha / 2, k, n - k + 1)
    hi = 1.0 if k == n else _beta_quantile(1 - alpha / 2, k + 1, n - k)
    return (lo, hi)


def _beta_quantile(p: float, a: float, b: float) -> float:
    """Find x such that I_x(a,b) = p, via bisection. Slow but stdlib-only."""
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _regularized_inc_beta(mid, a, b) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _regularized_inc_beta(x: float, a: float, b: float) -> float:
    """I_x(a,b). Lentz continued-fraction (Numerical Recipes §6.4)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(x, a, b) / a
    return 1.0 - front * _betacf(1 - x, b, a) / b


def _betacf(x: float, a: float, b: float, *, max_iter: int = 200) -> float:
    eps = 1e-15
    qab = a + b
    qap = a + 1
    qam = a - 1
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < eps:
        d = eps
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < eps:
            d = eps
        c = 1.0 + aa / c
        if abs(c) < eps:
            c = eps
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < eps:
            d = eps
        c = 1.0 + aa / c
        if abs(c) < eps:
            c = eps
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-10:
            return h
    return h


def sign_test(k_positive: int, n_total: int, *, alternative: str = "two-sided") -> float:
    """Exact binomial test that H_cond = 0.5. alternative in {two-sided, less, greater}."""
    if n_total == 0:
        return float("nan")
    if alternative == "greater":
        return _binom_sf(k_positive - 1, n_total, 0.5)
    if alternative == "less":
        return _binom_cdf(k_positive, n_total, 0.5)
    # two-sided: 2 * min(left, right), clipped
    p = 2 * min(_binom_cdf(k_positive, n_total, 0.5),
                _binom_sf(k_positive - 1, n_total, 0.5))
    return min(p, 1.0)


def _binom_pmf(k: int, n: int, p: float) -> float:
    if k < 0 or k > n:
        return 0.0
    return math.exp(math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
                    + k * math.log(p) + (n - k) * math.log(1 - p))


def _binom_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(_binom_pmf(i, n, p) for i in range(0, k + 1))


def _binom_sf(k: int, n: int, p: float) -> float:
    return 1.0 - _binom_cdf(k, n, p)


# ---------- Wilcoxon ----------

def wilcoxon_signed_rank(outcomes: list[float], *, alternative: str = "two-sided") -> float:
    """One-sample Wilcoxon signed-rank against 0. Approximate normal-z p-value."""
    nz = [o for o in outcomes if o != 0]
    n = len(nz)
    if n < 6:
        return float("nan")
    ranks = _rank_abs(nz)
    w_plus = sum(r for r, o in zip(ranks, nz) if o > 0)
    mu = n * (n + 1) / 4
    sigma2 = n * (n + 1) * (2 * n + 1) / 24
    # tie correction
    sigma2 -= _tie_correction(ranks)
    sigma = math.sqrt(max(sigma2, 1e-12))
    z = (w_plus - mu) / sigma
    if alternative == "greater":
        return 0.5 * math.erfc(z / math.sqrt(2))
    if alternative == "less":
        return 0.5 * math.erfc(-z / math.sqrt(2))
    return math.erfc(abs(z) / math.sqrt(2))


def _rank_abs(values: list[float]) -> list[float]:
    pairs = sorted(((abs(v), i) for i, v in enumerate(values)), key=lambda p: p[0])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[pairs[k][1]] = avg_rank
        i = j + 1
    return ranks


def _tie_correction(ranks: list[float]) -> float:
    from collections import Counter
    counts = Counter(ranks)
    return sum(t * (t * t - 1) / 48 for t in counts.values() if t > 1)


# ---------- Mann-Whitney U ----------

def mann_whitney_u(a: list[float], b: list[float]) -> float:
    """Two-sided p-value via normal approximation with tie correction."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return float("nan")
    combined = sorted(a + b)
    ranks = _rank_abs([v for v in combined])  # ranks regardless of sign
    # Build dict mapping value -> avg rank
    rank_of: dict[float, float] = {}
    pairs = sorted(((v, i) for i, v in enumerate(combined)), key=lambda p: p[0])
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        rank_of[pairs[i][0]] = r
        i = j + 1
    r1 = sum(rank_of[v] for v in a)
    u1 = r1 - n1 * (n1 + 1) / 2
    u2 = n1 * n2 - u1
    u = min(u1, u2)
    mu = n1 * n2 / 2
    sigma = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    if sigma <= 0:
        return float("nan")
    z = (u - mu) / sigma
    return math.erfc(abs(z) / math.sqrt(2))


# ---------- FDR ----------

def benjamini_hochberg(pvals: list[float], *, q: float = 0.05) -> list[bool]:
    """Return per-test reject decisions at FDR q. Order matches input."""
    if not pvals:
        return []
    n = len(pvals)
    indexed = sorted(enumerate(pvals), key=lambda p: p[1])
    # find largest k such that p_{(k)} <= (k/n) * q
    cutoff = -1
    for k, (_, p) in enumerate(indexed, start=1):
        if p <= (k / n) * q:
            cutoff = k
    decisions = [False] * n
    for k, (orig_i, _) in enumerate(indexed, start=1):
        if k <= cutoff:
            decisions[orig_i] = True
    return decisions


def benjamini_hochberg_adjusted(pvals: list[float]) -> list[float]:
    """BH-adjusted p-values (the values you'd compare to q directly)."""
    if not pvals:
        return []
    n = len(pvals)
    indexed = sorted(enumerate(pvals), key=lambda p: p[1])
    adjusted = [0.0] * n
    prev = 1.0
    for k in range(n, 0, -1):
        orig_i, p = indexed[k - 1]
        a = min(prev, p * n / k)
        adjusted[orig_i] = a
        prev = a
    return adjusted
