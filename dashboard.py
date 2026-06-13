"""Read-only status dashboard — plain-language, honest about what works.

Serves one page at PM_DASH_HOST:PM_DASH_PORT (default 127.0.0.1:8787). It is a
window onto the engine's SQLite state, nothing more — it never places orders,
never writes, and is safe to leave open.

Design intent: a non-finance reader should understand, in thirty seconds,
(1) whether the system is alive, (2) what each strategy is trying to do in
plain words, and (3) which strategies actually work versus which are dead
ends kept only as research. We deliberately do NOT show the things that
mislead: an AI model that doesn't beat a coin flip, or a "paper profit"
number computed from signals that have no edge.
"""
from __future__ import annotations

import time
from pathlib import Path

from aiohttp import web

from config.settings import Settings
from pm.core import db

STALE_AGE = 120.0  # a component quiet longer than this is flagged "stale"


# Plain-language description + honest standing for each strategy. The numbers
# are filled in live; the words are fixed and reviewed.
STRATEGY_INFO = {
    "struct_arb": {
        "name": "Risk-free arbitrage",
        "plain": "Buys every outcome of an event when together they cost less "
                 "than the $1 they're guaranteed to pay out. A locked-in profit "
                 "when it appears — but it appears rarely.",
        "tier": "real",
    },
    "whale_follow": {
        "name": "Copy proven wallets",
        "plain": "Watches the blockchain for big bettors, scores them as their "
                 "bets settle, and flags when a wallet with a winning track "
                 "record places a new bet.",
        "tier": "testing",
    },
    "news": {
        "name": "News reaction",
        "plain": "Reads live news headlines and flags a market when a headline "
                 "should move its price before the market catches up.",
        "tier": "live",
    },
    "calibration": {
        "name": "Model vs. market",
        "plain": "Compares the market price to an independent probability "
                 "estimate and flags large disagreements.",
        "tier": "needs_setup",
    },
    "microstructure": {
        "name": "Order-book patterns",
        "plain": "Looked for very short-term price patterns in the order book "
                 "(buying pressure, big trades, liquidity gaps).",
        "tier": "dead",
    },
    "momentum": {
        "name": "Price momentum",
        "plain": "Looked for prices that keep drifting one direction, or bounce "
                 "off the extremes near $0 and $1.",
        "tier": "dead",
    },
    "rel_value": {
        "name": "Price inconsistencies",
        "plain": "Looked for related contracts whose prices stopped adding up "
                 "the way they should.",
        "tier": "dead",
    },
}

# Plain-language label + colour for each tier.
TIER = {
    "real":        {"label": "Works — but rare",        "color": "good"},
    "testing":     {"label": "Collecting data",         "color": "info"},
    "live":        {"label": "Running",                 "color": "info"},
    "needs_setup": {"label": "Not set up",              "color": "muted"},
    "dead":        {"label": "No edge — research only",  "color": "muted"},
}

STRATEGY_ORDER = ["whale_follow", "struct_arb", "news", "calibration",
                  "microstructure", "momentum", "rel_value"]


def _scalar(conn, sql, *args):
    try:
        row = conn.execute(sql, args).fetchone()
    except Exception:  # noqa: BLE001 — table may not exist yet
        return 0
    return list(row)[0] if row and row[0] is not None else 0


def _whale_stats(conn) -> dict:
    return {
        "positions": _scalar(conn, "SELECT COUNT(*) FROM whale_positions"),
        "wallets": _scalar(conn, "SELECT COUNT(*) FROM whale_wallets"),
        "tracked": _scalar(conn, "SELECT COUNT(*) FROM whale_wallets WHERE tracked=1"),
        "resolved": _scalar(conn, "SELECT COUNT(*) FROM whale_positions WHERE outcome IS NOT NULL"),
    }


def _strategy_rows(conn) -> dict:
    rows = {}
    for r in conn.execute(
            "SELECT strategy, COUNT(*) n, "
            "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) labeled, "
            "AVG(outcome) avg_outcome, "
            "AVG(CASE WHEN outcome>0 THEN 1.0 WHEN outcome IS NOT NULL THEN 0.0 END) hit "
            "FROM signal_log GROUP BY strategy"):
        rows[r["strategy"]] = dict(r)
    return rows


def _status_line(strategy: str, srow: dict, whale: dict) -> str:
    """One plain-English sentence of current standing per strategy."""
    n = int(srow.get("n", 0) or 0)
    if strategy == "whale_follow":
        return (f"{whale['wallets']} wallets found, {whale['tracked']} have "
                f"earned a track record so far; {whale['resolved']} bets scored.")
    if strategy == "struct_arb":
        if n == 0:
            return "None seen yet — these are genuinely rare."
        return f"{n} found since launch (each is a one-off opportunity)."
    if strategy == "calibration":
        return "Needs a probability data source — none connected yet."
    if strategy == "news":
        return "Running. Fires only when a headline clearly matches a market."
    # dead-end research strategies
    labeled = int(srow.get("labeled", 0) or 0)
    if not labeled:
        return f"{n} recorded — kept for research only."
    avg = srow.get("avg_outcome") or 0.0
    direction = "no better than chance after fees" if avg <= 0.001 else "below the cost to trade it"
    return f"{n} recorded, {labeled} scored — {direction}."


def query_state(conn, settings) -> dict:
    now = time.time()

    components = []
    for r in conn.execute("SELECT component, ts, detail FROM heartbeats ORDER BY component"):
        age = now - r["ts"]
        components.append({"component": r["component"], "age_s": round(age, 1),
                           "ok": age <= STALE_AGE, "detail": r["detail"] or ""})

    hb_path = settings.heartbeat_path
    if hb_path.exists():
        hb_age = round(now - hb_path.stat().st_mtime, 1)
        engine_ok = hb_age <= settings.heartbeat_stale_after
    else:
        hb_age, engine_ok = None, False

    whale = _whale_stats(conn)
    srows = _strategy_rows(conn)

    strategies = []
    for strat in STRATEGY_ORDER:
        info = STRATEGY_INFO[strat]
        srow = srows.get(strat, {})
        tier = info["tier"]
        strategies.append({
            "key": strat,
            "name": info["name"],
            "plain": info["plain"],
            "tier": tier,
            "tier_label": TIER[tier]["label"],
            "tier_color": TIER[tier]["color"],
            "signals": int(srow.get("n", 0) or 0),
            "status": _status_line(strat, srow, whale),
        })

    counts = {
        "markets_active": _scalar(conn, "SELECT COUNT(*) FROM markets WHERE active=1 AND closed=0"),
        "markets_total": _scalar(conn, "SELECT COUNT(*) FROM markets"),
        "signals": _scalar(conn, "SELECT COUNT(*) FROM signal_log"),
        "real_trades": _scalar(conn, "SELECT COUNT(*) FROM execution_fills"),
    }

    return {
        "now": now,
        "engine": {"ok": engine_ok, "age_s": hb_age},
        "components": components,
        "components_ok": sum(1 for c in components if c["ok"]),
        "components_total": len(components),
        "counts": counts,
        "whale": whale,
        "strategies": strategies,
    }


async def handle_api(request: web.Request) -> web.Response:
    settings = request.app["settings"]
    conn = request.app["conn"]
    return web.json_response(query_state(conn, settings))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Prediction-market engine — status</title>
<style>
  :root{
    --bg:#f7f7f5; --panel:#fff; --line:#e4e4e0; --txt:#1c1c1a; --muted:#6b6b66;
    --good:#1d8a5e; --good-bg:#e6f4ec; --info:#1f6feb; --info-bg:#e8f0fe;
    --warn:#9a6a00; --warn-bg:#fbf0d9; --muted-bg:#efefec;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg:#161614; --panel:#1f1f1d; --line:#33332f; --txt:#ececea;
      --muted:#9a9a92; --good:#4ec38a; --good-bg:#13241c; --info:#6ea8ff;
      --info-bg:#15233d; --warn:#e0b25a; --warn-bg:#2a2310; --muted-bg:#26261f; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
  .wrap{max-width:820px;margin:0 auto;padding:28px 20px 60px;}
  h1{font-size:22px;font-weight:600;margin:0 0 4px;}
  .sub{color:var(--muted);margin:0 0 22px;font-size:15px;}
  .banner{display:flex;align-items:center;gap:12px;background:var(--panel);
    border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin-bottom:22px;}
  .big-dot{width:12px;height:12px;border-radius:50%;flex:none;}
  .ok-dot{background:var(--good)} .bad-dot{background:#d4564f}
  .banner b{font-weight:600}
  .stat-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:28px;}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}
  .stat .v{font-size:24px;font-weight:600;}
  .stat .l{font-size:13px;color:var(--muted);margin-top:2px;}
  h2{font-size:17px;font-weight:600;margin:30px 0 12px;}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:16px 18px;margin-bottom:12px;}
  .card-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;}
  .card-top .nm{font-weight:600;font-size:16px;}
  .badge{font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;white-space:nowrap;}
  .badge.good{background:var(--good-bg);color:var(--good);}
  .badge.info{background:var(--info-bg);color:var(--info);}
  .badge.warn{background:var(--warn-bg);color:var(--warn);}
  .badge.muted{background:var(--muted-bg);color:var(--muted);}
  .plain{color:var(--txt);margin:0 0 8px;font-size:14.5px;}
  .status{color:var(--muted);font-size:13.5px;margin:0;}
  .count-chip{margin-left:auto;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums;}
  details{margin-top:24px;border-top:1px solid var(--line);padding-top:14px;}
  summary{cursor:pointer;color:var(--muted);font-size:14px;}
  .comp{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:13px;
    border-bottom:1px solid var(--line);}
  .comp:last-child{border-bottom:none}
  .comp .d{width:8px;height:8px;border-radius:50%;flex:none;}
  .comp .nm{min-width:130px;font-weight:500;}
  .comp .dt{color:var(--muted);font-size:12px;}
  .foot{color:var(--muted);font-size:12px;margin-top:26px;text-align:center;}
  .learned{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:16px 18px;font-size:14.5px;}
  .learned p{margin:0 0 10px;} .learned p:last-child{margin:0;}
  .learned b{font-weight:600;}
</style></head>
<body><div class="wrap">
  <h1>Prediction-market engine</h1>
  <p class="sub">A research tool that watches Polymarket and tests ways to predict
    price moves. It places <b>no real trades</b> — everything here is observation.</p>

  <div class="banner">
    <span id="bigdot" class="big-dot ok-dot"></span>
    <div><b id="bannermain">Loading…</b><div class="sub" style="margin:0" id="bannersub"></div></div>
  </div>

  <div class="stat-row" id="stats"></div>

  <h2>What it's testing</h2>
  <div id="strategies"></div>

  <h2>What we've learned so far</h2>
  <div class="learned">
    <p><b>One thing clearly works but is rare:</b> risk-free arbitrage — when the
      pieces of an event sell for less than they're guaranteed to pay. Real, but
      shows up only occasionally.</p>
    <p><b>The short-term order-book patterns were a dead end.</b> After the small
      trading fee, they don't beat a coin flip. We keep recording them for the
      write-up, but they're not worth trading.</p>
    <p><b>The current live experiment is copying proven wallets.</b> The system is
      watching big blockchain bettors and scoring them as their bets settle. In a
      few days we'll know whether any of them are reliably skilled.</p>
    <p><b>Nothing is wired to spend money.</b> Trading stays switched off until a
      strategy proves itself and is reviewed.</p>
  </div>

  <details>
    <summary>System health (for the technical reader)</summary>
    <div id="components" style="margin-top:12px"></div>
  </details>

  <p class="foot" id="foot"></p>
</div>
<script>
function ago(s){ if(s==null) return "never"; if(s<60) return Math.round(s)+"s ago";
  if(s<3600) return Math.round(s/60)+"m ago"; return Math.round(s/3600)+"h ago"; }
function num(n){ return (n||0).toLocaleString(); }

async function refresh(){
  let d;
  try{ d = await (await fetch("/api/state")).json(); }
  catch(e){ document.getElementById("bannermain").textContent="Dashboard can't reach the engine."; return; }

  const engineUp = d.engine.ok;
  const allOk = engineUp && d.components_ok === d.components_total;
  document.getElementById("bigdot").className = "big-dot " + (allOk ? "ok-dot" : "bad-dot");
  document.getElementById("bannermain").textContent = engineUp
    ? (allOk ? "Everything is running normally." : "Running, with one or more parts catching up.")
    : "The engine is not running.";
  document.getElementById("bannersub").textContent =
    d.components_ok + " of " + d.components_total + " parts healthy · engine heartbeat " + ago(d.engine.age_s);

  const c = d.counts;
  document.getElementById("stats").innerHTML = [
    ["Markets watched", num(c.markets_active)],
    ["Signals recorded", num(c.signals)],
    ["Wallets found", num(d.whale.wallets)],
    ["Real trades", num(c.real_trades) + (c.real_trades===0 ? " · off" : "")],
  ].map(([l,v]) => `<div class="stat"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");

  document.getElementById("strategies").innerHTML = d.strategies.map(s => `
    <div class="card">
      <div class="card-top">
        <span class="nm">${s.name}</span>
        <span class="badge ${s.tier_color}">${s.tier_label}</span>
        <span class="count-chip">${num(s.signals)} signals</span>
      </div>
      <p class="plain">${s.plain}</p>
      <p class="status">${s.status}</p>
    </div>`).join("");

  document.getElementById("components").innerHTML = d.components.map(c => `
    <div class="comp">
      <span class="d" style="background:${c.ok ? 'var(--good)' : '#d4564f'}"></span>
      <span class="nm">${c.component}</span>
      <span class="dt">${ago(c.age_s)}${c.detail ? " · " + c.detail : ""}</span>
    </div>`).join("");

  document.getElementById("foot").textContent = "Updated " + new Date().toLocaleTimeString()
    + " · refreshes every 5s · read-only";
}
refresh(); setInterval(refresh, 5000);
</script>
</body></html>
"""


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


def make_app(settings: Settings) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["conn"] = db.connect(settings.db_path)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api)
    return app


def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    app = make_app(settings)
    logging.getLogger(__name__).info(
        "dashboard at http://%s:%d", settings.dashboard_host, settings.dashboard_port)
    web.run_app(app, host=settings.dashboard_host, port=settings.dashboard_port,
                print=None)


if __name__ == "__main__":
    main()
