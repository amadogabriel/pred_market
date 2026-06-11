import collections
import json
import sqlite3
import time

c = sqlite3.connect("data/state.db")
c.row_factory = sqlite3.Row

print("--- heartbeats ---")
now = time.time()
for r in c.execute("SELECT component, ts, detail FROM heartbeats ORDER BY component"):
    print(f"  {r['component']:<14} age={now - r['ts']:6.1f}s  {r['detail']}")

print("--- signals by strategy/kind ---")
rows = c.execute(
    "SELECT strategy, kind, COUNT(*) n, AVG(gross_edge) avg_gross "
    "FROM signal_log GROUP BY strategy, kind").fetchall()
if not rows:
    print("  (none fired)")
for r in rows:
    print(f"  {r['strategy']:<16} {r['kind']:<22} n={r['n']:<4} avg_gross={r['avg_gross']:.4f}")

print("--- sample research signals ---")
for r in c.execute("SELECT signal_id, strategy, kind, group_id, gross_edge, net_edge, "
                   "features_json FROM signal_log WHERE strategy != 'struct_arb' "
                   "ORDER BY signal_id DESC LIMIT 5"):
    feats = json.loads(r["features_json"] or "{}")
    print(f"  #{r['signal_id']} {r['strategy']}/{r['kind']} gross={r['gross_edge']:.4f} "
          f"net={r['net_edge']:.4f}")
    print(f"     {feats}")

print("--- execution traces (should be 0 with defaults) ---")
print("  intents:", c.execute("SELECT COUNT(*) FROM execution_intents").fetchone()[0])
print("  risk_events:", c.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0])

print("--- labeled outcomes ---")
print("  labeled:", c.execute("SELECT COUNT(*) FROM signal_log WHERE outcome IS NOT NULL").fetchone()[0],
      "of", c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0])
