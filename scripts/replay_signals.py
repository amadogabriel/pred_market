"""Replay the event log through the research scanners and evaluate outcomes.

Tune thresholds offline instead of waiting through live soak:

    python scripts/replay_signals.py
    python scripts/replay_signals.py --ofi 0.5 --z 2.5 --horizon 600
    python scripts/replay_signals.py --min-samples 10 --rv-min-samples 15
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402
from pm.core import db  # noqa: E402
from pm.backtest.signal_replay import replay_signals  # noqa: E402
from pm.execution.fee_engine import FeeEngine  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    p = argparse.ArgumentParser(description="Offline research-signal replay.")
    p.add_argument("--db", type=Path, default=settings.db_path)
    p.add_argument("--events-dir", type=Path, default=settings.events_dir)
    p.add_argument("--horizon", type=float, default=settings.label_horizon_s)
    p.add_argument("--max-events", type=int, default=None)
    # microstructure overrides
    p.add_argument("--ofi", type=float, default=settings.micro_ofi_threshold)
    p.add_argument("--min-samples", type=int, default=settings.micro_min_samples)
    p.add_argument("--max-spread", type=float, default=settings.micro_max_spread)
    p.add_argument("--debounce", type=float, default=settings.micro_debounce_s)
    # relative-value overrides
    p.add_argument("--z", type=float, default=settings.rv_z_threshold)
    p.add_argument("--rv-min-samples", type=int, default=settings.rv_min_samples)
    p.add_argument("--rv-min-dev", type=float, default=settings.rv_min_abs_dev)
    args = p.parse_args(argv)

    conn = db.connect(args.db)
    fees = FeeEngine.from_yaml(settings.fees_yaml)
    result = replay_signals(
        args.events_dir, conn, fees,
        horizon_s=args.horizon, max_events=args.max_events,
        micro_kwargs={
            "ofi_threshold": args.ofi, "min_samples": args.min_samples,
            "max_spread": args.max_spread, "debounce_s": args.debounce,
            "window_s": settings.micro_window_s,
            "liq_spread_mult": settings.micro_liq_spread_mult,
            "liq_depth_drop": settings.micro_liq_depth_drop,
            "trade_abs_floor": settings.micro_trade_abs_floor,
        },
        rv_kwargs={
            "z_threshold": args.z, "min_samples": args.rv_min_samples,
            "min_abs_dev": args.rv_min_dev, "debounce_s": settings.rv_debounce_s,
            "window_s": settings.rv_window_s,
        })

    print(f"replayed {result.events} market events, "
          f"{len(result.signals)} research signals emitted")
    print(f"forward-return horizon: {args.horizon:.0f}s "
          f"(mid drift; ignores spread/impact)\n")
    header = f"{'strategy':<16} {'kind':<22} {'n':>5} {'labeled':>8} {'hit':>6} {'avg':>9} {'median':>9}"
    print(header)
    print("-" * len(header))
    for (strategy, kind), s in sorted(result.stats.items()):
        hit = f"{s.hit_rate:.0%}" if s.hit_rate is not None else "—"
        avg = f"{s.avg_outcome:+.4f}" if s.avg_outcome is not None else "—"
        med = f"{s.median_outcome:+.4f}" if s.median_outcome is not None else "—"
        print(f"{strategy:<16} {kind:<22} {s.n:>5} {s.labeled:>8} {hit:>6} {avg:>9} {med:>9}")
    if not result.stats:
        print("(no signals at these thresholds)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
