"""Produce the preregistered research report from signal_log.

Reads `data/state.db.signal_log`, runs the analysis plan from
`research/STATISTICS.md`, and prints the per-kind results table that
appears in the paper.

    python scripts/research_report.py
    python scripts/research_report.py --boot 20000   # tighter CIs
    python scripts/research_report.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pm.research import stats  # noqa: E402


KINDS = [
    # (strategy, kind, alt) — alt: 'greater' | 'less' | 'two-sided'
    ("struct_arb", "partition_buy_all", "greater"),
    ("struct_arb", "partition_sell_all", "greater"),
    ("struct_arb", "complement", "greater"),
    ("microstructure", "ofi_pressure", "greater"),
    ("microstructure", "liquidity_shock", "two-sided"),
    ("microstructure", "trade_through", "two-sided"),  # contrarian hypothesis
    ("rel_value", "complement_drift", "greater"),
    ("rel_value", "partition_sum_drift", "greater"),
    ("momentum", "directional_momentum", "two-sided"),
    ("momentum", "boundary_overshoot", "greater"),
]


def fetch_outcomes(conn: sqlite3.Connection, strategy: str,
                   kind: str) -> tuple[int, list[float]]:
    n = conn.execute(
        "SELECT COUNT(*) FROM signal_log WHERE strategy=? AND kind=?",
        (strategy, kind)).fetchone()[0]
    rows = conn.execute(
        "SELECT outcome FROM signal_log WHERE strategy=? AND kind=? "
        "AND outcome IS NOT NULL ORDER BY signal_id", (strategy, kind))
    outcomes = [r[0] for r in rows]
    return n, outcomes


def analyse_kind(strategy: str, kind: str, alt: str, n_total: int,
                 outcomes: list[float], *, n_boot: int, rng: random.Random) -> dict:
    s = stats.summarise(outcomes)
    if not outcomes:
        return {"strategy": strategy, "kind": kind, "n": n_total, **s,
                "ci_h_cond": (None, None), "ci_mean": (None, None),
                "p_raw": None, "p_adj": None, "verdict": "no signals"}

    # Hit-rate test on the moved subset
    moved = [o for o in outcomes if o != 0]
    pos_moved = sum(1 for o in moved if o > 0)
    if moved:
        p_raw = stats.sign_test(pos_moved, len(moved), alternative=alt)
        cp_lo, cp_hi = stats.clopper_pearson(pos_moved, len(moved))
        ci_h = (cp_lo, cp_hi)
    else:
        p_raw = float("nan")
        ci_h = (None, None)

    # Bootstrap CI on mean outcome
    ci_mean = stats.bootstrap_ci(
        outcomes, lambda xs: sum(xs) / len(xs), n_boot=n_boot, rng=rng)

    return {"strategy": strategy, "kind": kind, "n": n_total, **s,
            "ci_h_cond": ci_h, "ci_mean": ci_mean,
            "p_raw": p_raw, "p_adj": None, "verdict": None}


def verdict(row: dict, n_min_decisive: int = 30) -> str:
    if row["n_lab"] == 0:
        return "no labeled signals"
    if row["n_moved"] < n_min_decisive:
        return f"underpowered (n_moved={row['n_moved']})"
    if row["p_adj"] is None or row["p_adj"] != row["p_adj"]:  # NaN
        return "no test"
    if row["p_adj"] > 0.05:
        return "not supported"
    # supported -- but trade-through is special: contrarian = supported in reverse direction
    if row["strategy"] == "microstructure" and row["kind"] == "trade_through":
        return "inverted (contrarian)" if (row["h_cond"] or 0) < 0.5 else "supported"
    return "supported"


def fmt(v, spec="+.4f", none="—"):
    if v is None or (isinstance(v, float) and v != v):
        return none
    return format(v, spec)


def print_table(rows: list[dict]) -> None:
    cols = [
        ("strategy/kind", 32, lambda r: f"{r['strategy']}/{r['kind']}"),
        ("n", 5, lambda r: r["n"]),
        ("n_lab", 6, lambda r: r["n_lab"]),
        ("n_mv", 5, lambda r: r["n_moved"]),
        ("H_cond [95% CI]", 22, lambda r: f"{fmt(r['h_cond'], '.3f')} "
                                          f"[{fmt(r['ci_h_cond'][0], '.3f')}, "
                                          f"{fmt(r['ci_h_cond'][1], '.3f')}]"
                                          if r["h_cond"] is not None else "—"),
        ("mean [95% CI]", 26, lambda r: f"{fmt(r['mean'])} "
                                        f"[{fmt(r['ci_mean'][0])}, "
                                        f"{fmt(r['ci_mean'][1])}]"
                                        if r["mean"] is not None else "—"),
        ("p_raw", 9, lambda r: fmt(r["p_raw"], ".4f")),
        ("p_adj", 9, lambda r: fmt(r["p_adj"], ".4f")),
        ("verdict", 24, lambda r: r["verdict"]),
    ]
    header = " ".join(f"{h:<{w}}" for h, w, _ in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print(" ".join(f"{str(extract(r)):<{w}}" for _, w, extract in cols))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=ROOT / "data" / "state.db")
    p.add_argument("--boot", type=int, default=10_000)
    p.add_argument("--json", type=Path)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    rng = random.Random(args.seed)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    raw_rows: list[dict] = []
    for strategy, kind, alt in KINDS:
        n_total, outcomes = fetch_outcomes(conn, strategy, kind)
        raw_rows.append(analyse_kind(strategy, kind, alt, n_total, outcomes,
                                     n_boot=args.boot, rng=rng))

    # FDR correction across kinds with a valid p-value
    indices = [i for i, r in enumerate(raw_rows)
               if r["p_raw"] is not None and r["p_raw"] == r["p_raw"]]
    if indices:
        ps = [raw_rows[i]["p_raw"] for i in indices]
        adj = stats.benjamini_hochberg_adjusted(ps)
        for k, i in enumerate(indices):
            raw_rows[i]["p_adj"] = adj[k]

    for r in raw_rows:
        r["verdict"] = verdict(r)

    print(f"\npm-system research report — db={args.db}")
    print(f"bootstrap resamples={args.boot}  seed={args.seed}  "
          f"FDR family={len(indices)} kinds, q=0.05\n")
    print_table(raw_rows)

    print("\nNotes:")
    print("- H_cond is the hit rate conditioned on outcome != 0 (tick-size aware).")
    print("- p_adj is Benjamini-Hochberg FDR-corrected across kinds with valid tests.")
    print("- 'underpowered' = n_moved < 30; not a failure, just insufficient data.")
    print("- See research/HYPOTHESES.md for the preregistered decision rules.\n")

    if args.json:
        # JSON-safe view
        out = []
        for r in raw_rows:
            out.append({k: (list(v) if isinstance(v, tuple) else v)
                        for k, v in r.items()})
        args.json.write_text(json.dumps(out, indent=2))
        print(f"wrote JSON to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
