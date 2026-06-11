"""pm-system dashboard — read-only web UI.

A small aiohttp server that reads the operational state DB and serves a
single auto-refreshing page plus a JSON API (`/api/state`). It never writes:
purely an observability surface over the same `state.db` the engine maintains
(SQLite WAL allows concurrent readers). Run it as a third process alongside
the engine and monitor.

    python dashboard.py        # then open http://127.0.0.1:8787
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from aiohttp import web

from config.settings import Settings
from pm.core import db

log = logging.getLogger(__name__)

STALE_AGE = 120.0  # seconds; matches monitor's component-staleness threshold


def _event_log_status(settings) -> dict:
    """Size + freshness of today's event-log file (cheap; no line count)."""
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = Path(settings.events_dir) / day / "events.jsonl"
    if not path.exists():
        return {"exists": False, "day": day, "size_mb": 0.0, "age_s": None}
    st = path.stat()
    return {
        "exists": True,
        "day": day,
        "size_mb": round(st.st_size / 1_048_576, 3),
        "age_s": round(time.time() - st.st_mtime, 1),
    }


def query_state(conn, settings) -> dict:
    now = time.time()

    # component heartbeats
    components = []
    for r in conn.execute("SELECT component, ts, detail FROM heartbeats ORDER BY component"):
        age = now - r["ts"]
        components.append({
            "component": r["component"],
            "age_s": round(age, 1),
            "stale": age > STALE_AGE,
            "detail": r["detail"] or "",
        })

    # engine heartbeat file
    hb_path = settings.heartbeat_path
    if hb_path.exists():
        hb_age = round(now - hb_path.stat().st_mtime, 1)
        engine_hb = {"exists": True, "age_s": hb_age, "stale": hb_age > settings.heartbeat_stale_after}
    else:
        engine_hb = {"exists": False, "age_s": None, "stale": True}

    def scalar(sql, *args):
        row = conn.execute(sql, args).fetchone()
        return list(row)[0] if row else 0

    counts = {
        "markets": scalar("SELECT COUNT(*) FROM markets"),
        "neg_risk_groups": scalar(
            "SELECT COUNT(*) FROM (SELECT neg_risk_id FROM markets "
            "WHERE neg_risk_id IS NOT NULL GROUP BY neg_risk_id HAVING COUNT(*)>1)"),
        "signals": scalar("SELECT COUNT(*) FROM signal_log"),
        "recon_rows": scalar("SELECT COUNT(*) FROM recon_log"),
        "rules_versions": scalar("SELECT COUNT(*) FROM rules_text"),
    }

    # recon drift summary (last hour)
    since = now - 3600
    recon = {
        "max_abs_diff": round(scalar(
            "SELECT COALESCE(MAX(ABS(diff)),0) FROM recon_log WHERE diff IS NOT NULL"), 4),
        "drift_count": scalar(
            "SELECT COUNT(*) FROM recon_log WHERE ABS(COALESCE(diff,0))>0.02 AND ts>?", since),
        "recent": scalar("SELECT COUNT(*) FROM recon_log WHERE ts>?", since),
    }

    # category breakdown
    categories = [
        {"category": (r["category"] or "(none)"), "n": r["n"]}
        for r in conn.execute(
            "SELECT category, COUNT(*) n FROM markets GROUP BY category ORDER BY n DESC LIMIT 10")
    ]

    # recent signals
    signals = [dict(r) for r in conn.execute(
        "SELECT signal_id, kind, group_id, net_edge, exec_sets, ts FROM signal_log "
        "ORDER BY ts DESC LIMIT 15")]

    # top tracked markets by liquidity
    top_markets = [dict(r) for r in conn.execute(
        "SELECT question, category, liquidity, neg_risk FROM markets "
        "WHERE active=1 AND closed=0 ORDER BY COALESCE(liquidity,0) DESC LIMIT 12")]

    return {
        "now": now,
        "engine_heartbeat": engine_hb,
        "components": components,
        "counts": counts,
        "recon": recon,
        "categories": categories,
        "signals": signals,
        "top_markets": top_markets,
        "event_log": _event_log_status(settings),
    }


async def handle_api(request: web.Request) -> web.Response:
    state = query_state(request.app["conn"], request.app["settings"])
    return web.json_response(state)


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pm-system dashboard</title>
<style>
  :root {
    --bg:#0b0e14; --panel:#141925; --panel2:#1b2230; --line:#283142;
    --txt:#e6edf3; --muted:#8b98a9; --green:#3fb950; --red:#f85149;
    --amber:#d29922; --accent:#58a6ff;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
    font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif; }
  header { display:flex; align-items:center; gap:14px; padding:16px 22px;
    border-bottom:1px solid var(--line); background:var(--panel); position:sticky; top:0; }
  header h1 { font-size:16px; margin:0; font-weight:600; letter-spacing:.3px; }
  header .sub { color:var(--muted); font-size:12px; }
  header .pill { margin-left:auto; font-size:12px; color:var(--muted); }
  .signal-only { background:rgba(210,153,34,.15); color:var(--amber);
    border:1px solid rgba(210,153,34,.4); padding:3px 9px; border-radius:999px; font-size:11px; font-weight:600; }
  main { padding:22px; max-width:1200px; margin:0 auto; }
  .grid { display:grid; gap:14px; }
  .cards { grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); margin-bottom:18px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .card .label { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.6px; }
  .card .value { font-size:26px; font-weight:650; margin-top:4px; }
  .card .value small { font-size:13px; color:var(--muted); font-weight:400; }
  section { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:16px 18px; margin-bottom:18px; }
  section h2 { font-size:13px; margin:0 0 12px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.6px; font-weight:600; }
  .two { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
  @media (max-width:820px){ .two{ grid-template-columns:1fr; } }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; font-size:11px; text-transform:uppercase; letter-spacing:.4px; }
  td.num,th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:8px; vertical-align:middle; }
  .dot.ok { background:var(--green); box-shadow:0 0 7px var(--green); }
  .dot.bad { background:var(--red); box-shadow:0 0 7px var(--red); }
  .comp-row { display:flex; align-items:center; padding:8px 0; border-bottom:1px solid var(--line); }
  .comp-row:last-child { border-bottom:none; }
  .comp-name { font-weight:600; width:150px; }
  .comp-detail { color:var(--muted); font-size:12px; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .comp-age { color:var(--muted); font-variant-numeric:tabular-nums; }
  .empty { color:var(--muted); font-style:italic; padding:10px 0; }
  .bar { height:6px; border-radius:3px; background:var(--panel2); overflow:hidden; margin-top:5px; }
  .bar > i { display:block; height:100%; background:var(--accent); }
  .cat-row { display:flex; justify-content:space-between; font-size:12px; padding:2px 0; }
  .tag { font-size:10px; padding:1px 6px; border-radius:4px; background:var(--panel2); color:var(--muted); }
  .err { color:var(--red); }
</style>
</head>
<body>
<header>
  <h1>pm-system</h1>
  <span class="sub">Phase 0 · Polymarket</span>
  <span class="signal-only">SIGNAL-ONLY · no live orders</span>
  <span class="pill" id="updated">connecting…</span>
</header>
<main>
  <div class="grid cards" id="cards"></div>
  <div class="two">
    <section>
      <h2>Component health</h2>
      <div id="components"></div>
    </section>
    <section>
      <h2>Recon (WS vs REST)</h2>
      <div id="recon"></div>
      <h2 style="margin-top:18px">Categories</h2>
      <div id="categories"></div>
    </section>
  </div>
  <section>
    <h2>Recent signals</h2>
    <div id="signals"></div>
  </section>
  <section>
    <h2>Top tracked markets by liquidity</h2>
    <div id="markets"></div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
const fmtAge = s => s == null ? "—" : (s < 90 ? s.toFixed(0)+"s" : (s/60).toFixed(1)+"m");
const fmtUsd = v => v == null ? "—" : "$" + Math.round(v).toLocaleString();
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

function card(label, value, sub) {
  return `<div class="card"><div class="label">${label}</div>
    <div class="value">${value}${sub ? ` <small>${sub}</small>` : ""}</div></div>`;
}

async function refresh() {
  let s;
  try { s = await (await fetch("/api/state")).json(); }
  catch (e) { $("updated").innerHTML = '<span class="err">engine/db unreachable</span>'; return; }

  const hb = s.engine_heartbeat, el = s.event_log;
  $("cards").innerHTML =
    card("Markets", s.counts.markets.toLocaleString()) +
    card("NegRisk groups", s.counts.neg_risk_groups.toLocaleString()) +
    card("Signals fired", s.counts.signals.toLocaleString()) +
    card("Recon rows", s.counts.recon_rows.toLocaleString()) +
    card("Engine HB", (hb.stale ? '<span class="err">stale</span>' : "live"), fmtAge(hb.age_s)) +
    card("Event log", el.exists ? el.size_mb + " MB" : '<span class="err">none</span>',
         el.exists ? fmtAge(el.age_s) + " ago" : "");

  $("components").innerHTML = s.components.length ? s.components.map(c =>
    `<div class="comp-row"><span class="dot ${c.stale?'bad':'ok'}"></span>
      <span class="comp-name">${esc(c.component)}</span>
      <span class="comp-detail">${esc(c.detail)}</span>
      <span class="comp-age">${fmtAge(c.age_s)}</span></div>`).join("")
    : '<div class="empty">no heartbeats yet</div>';

  const r = s.recon;
  const driftClass = r.max_abs_diff > 0.02 ? "err" : "";
  $("recon").innerHTML =
    `<table><tr><th>max |diff|</th><th class="num">drift events (1h)</th><th class="num">rows (1h)</th></tr>
     <tr><td class="${driftClass}">${r.max_abs_diff.toFixed(4)}</td>
     <td class="num">${r.drift_count}</td><td class="num">${r.recent}</td></tr></table>`;

  const maxCat = Math.max(1, ...s.categories.map(c => c.n));
  $("categories").innerHTML = s.categories.length ? s.categories.map(c =>
    `<div class="cat-row"><span>${esc(c.category)}</span><span>${c.n.toLocaleString()}</span></div>
     <div class="bar"><i style="width:${(c.n/maxCat*100).toFixed(1)}%"></i></div>`).join("")
    : '<div class="empty">no markets yet</div>';

  $("signals").innerHTML = s.signals.length ?
    `<table><tr><th>id</th><th>kind</th><th>group</th><th class="num">net edge</th>
       <th class="num">sets</th><th class="num">age</th></tr>` +
    s.signals.map(g => `<tr><td>${g.signal_id}</td><td>${esc(g.kind)}</td>
      <td>${esc((g.group_id||"").slice(0,18))}…</td>
      <td class="num">${g.net_edge.toFixed(4)}</td><td class="num">${g.exec_sets.toFixed(1)}</td>
      <td class="num">${fmtAge(s.now - g.ts)}</td></tr>`).join("") + `</table>`
    : '<div class="empty">no signals fired yet — expected when no arb exists (system is signal-only)</div>';

  $("markets").innerHTML = s.top_markets.length ?
    `<table><tr><th>question</th><th>category</th><th>negrisk</th><th class="num">liquidity</th></tr>` +
    s.top_markets.map(m => `<tr><td>${esc((m.question||"").slice(0,70))}</td>
      <td><span class="tag">${esc(m.category||"—")}</span></td>
      <td>${m.neg_risk ? "✓" : ""}</td>
      <td class="num">${fmtUsd(m.liquidity)}</td></tr>`).join("") + `</table>`
    : '<div class="empty">no markets yet</div>';

  const d = new Date(s.now * 1000);
  $("updated").textContent = "updated " + d.toLocaleTimeString();
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def make_app(settings) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["conn"] = db.connect(settings.db_path)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    app = make_app(settings)
    log.info("dashboard at http://%s:%d", settings.dashboard_host, settings.dashboard_port)
    web.run_app(app, host=settings.dashboard_host, port=settings.dashboard_port,
                print=None)


if __name__ == "__main__":
    main()
