"""Print a concise operational report from the state DB and event logs."""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402
from pm.core import db  # noqa: E402
from pm.backtest.replay import summarize_events  # noqa: E402


def _connect(db_path: Path) -> sqlite3.Connection:
    return db.connect(db_path)


def _scalar(conn: sqlite3.Connection, sql: str, *args) -> float:
    row = conn.execute(sql, args).fetchone()
    return 0 if row is None else list(row)[0]


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(description="Print a pm-system daily report.")
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--events-dir", type=Path, default=settings.events_dir)
    args = parser.parse_args(argv)

    conn = _connect(args.db)
    since = time.time() - 86400.0
    events = summarize_events(args.events_dir)

    print("pm-system daily report")
    print("======================")
    print(f"markets:           {_scalar(conn, 'SELECT COUNT(*) FROM markets')}")
    print(f"signals total:     {_scalar(conn, 'SELECT COUNT(*) FROM signal_log')}")
    print(f"signals 24h:       {_scalar(conn, 'SELECT COUNT(*) FROM signal_log WHERE ts > ?', since)}")
    print(f"exec intents:      {_scalar(conn, 'SELECT COUNT(*) FROM execution_intents')}")
    print(f"fills:             {_scalar(conn, 'SELECT COUNT(*) FROM execution_fills')}")
    print(f"risk events 24h:   {_scalar(conn, 'SELECT COUNT(*) FROM risk_events WHERE ts > ?', since)}")
    print(f"recon max |diff|:  {_scalar(conn, 'SELECT COALESCE(MAX(ABS(diff)), 0) FROM recon_log'):.4f}")
    print(f"event files:       {events.files}")
    print(f"event rows:        {events.events}")
    print("component ages:")
    now = time.time()
    for row in conn.execute("SELECT component, ts, detail FROM heartbeats ORDER BY component"):
        detail = f" ({row['detail']})" if row["detail"] else ""
        print(f"  {row['component']:<16} {now - row['ts']:.1f}s{detail}")

    print()
    print("signal performance (labeler outcomes, all time)")
    print("-----------------------------------------------")
    rows = conn.execute(
        "SELECT strategy, kind, COUNT(*) n, "
        "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) labeled, "
        "AVG(outcome) avg_outcome, "
        "AVG(CASE WHEN outcome > 0 THEN 1.0 WHEN outcome IS NOT NULL THEN 0.0 END) hit_rate "
        "FROM signal_log GROUP BY strategy, kind ORDER BY strategy, kind").fetchall()
    if not rows:
        print("  no signals yet")
    for r in rows:
        if r["labeled"]:
            perf = (f"hit_rate={r['hit_rate']:.0%} avg_fwd_edge={r['avg_outcome']:+.4f} "
                    f"(labeled {r['labeled']}/{r['n']})")
        else:
            perf = f"unlabeled ({r['n']} signals)"
        print(f"  {r['strategy']:<16} {r['kind']:<22} {perf}")
    print()
    print("note: research signals (exec_sets=0) are hypotheses; promote to the")
    print("execution allowlist only after sustained positive labeled outcomes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
