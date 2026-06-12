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
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote_plus

from aiohttp import web

from config.settings import Settings
from pm.core import db

log = logging.getLogger(__name__)

STALE_AGE = 120.0  # seconds; matches monitor's component-staleness threshold
PAPER_DECISION_LIMIT = 20


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


def _paper_portfolio(conn, settings) -> dict:
    """Replay executable signal rows into a small read-only paper portfolio."""
    market_meta: dict[str, dict] = {}

    def market_info(market_id: str | None) -> dict:
        if not market_id:
            return {"slug": "", "question": "", "url": ""}
        if market_id not in market_meta:
            row = conn.execute(
                "SELECT slug, question FROM markets WHERE market_id=?", (market_id,)).fetchone()
            slug = str(row["slug"] or "") if row else ""
            question = str(row["question"] or "") if row else ""
            search_query = question or str(market_id)
            market_meta[market_id] = {
                "slug": slug,
                "question": question,
                "url": (
                    f"https://polymarket.com/market/{slug}" if slug
                    else f"https://polymarket.com/search?query={quote_plus(search_query)}"
                ),
            }
        return market_meta[market_id]

    def linked_legs(legs: list[dict]) -> list[dict]:
        out = []
        for leg in legs:
            market_id = leg.get("market_id")
            meta = market_info(market_id)
            out.append({
                "token_id": str(leg.get("token_id", "")),
                "market_id": str(market_id or ""),
                "side": str(leg.get("side", "")).upper(),
                "url": meta["url"],
                "slug": meta["slug"],
                "question": meta["question"],
            })
        return out

    bankroll = float(settings.paper_portfolio_usd)
    cash = bankroll
    realized_pnl = 0.0
    deployed_notional = 0.0
    sold_notional = 0.0
    positions: dict[str, dict] = {}
    decisions: list[dict] = []
    picked = defaultdict(lambda: {
        "selected": 0, "notional": 0.0, "paper_pnl": 0.0, "sim_pnl": 0.0,
    })

    strategy_rows = {
        (r["strategy"], r["kind"]): {
            "strategy": r["strategy"],
            "kind": r["kind"],
            "signals": int(r["signals"]),
            "executable": int(r["executable"]),
            "selected": 0,
            "notional": 0.0,
            "paper_pnl": 0.0,
            "sim_pnl": 0.0,
            "status": (
                "paper eligible" if int(r["executable"]) > 0 else "research only"
            ),
        }
        for r in conn.execute(
            "SELECT strategy, kind, COUNT(*) signals, "
            "SUM(CASE WHEN exec_sets > 0 THEN 1 ELSE 0 END) executable "
            "FROM signal_log GROUP BY strategy, kind ORDER BY strategy, kind")
    }

    def add_decision(row, *, status: str, action: str, reason: str,
                     sets: float = 0.0, notional: float = 0.0,
                     cost_per_set: float = 0.0, paper_pnl: float = 0.0,
                     sim_pnl: float = 0.0, legs: list[dict] | None = None) -> None:
        decisions.append({
            "signal_id": row["signal_id"],
            "strategy": row["strategy"],
            "kind": row["kind"],
            "action": action,
            "status": status,
            "reason": reason,
            "sets": round(sets, 4),
            "notional": round(notional, 4),
            "cost_per_set": round(cost_per_set, 4),
            "net_edge": round(float(row["net_edge"] or 0.0), 4),
            "paper_pnl": round(paper_pnl, 4),
            "sim_pnl": round(sim_pnl, 4),
            "tokens": linked_legs(legs or []),
            "ts": row["ts"],
        })

    rows = conn.execute(
        "SELECT signal_id, strategy, kind, group_id, legs_json, net_edge, "
        "exec_sets, outcome, pnl, ts FROM signal_log "
        "WHERE exec_sets > 0 ORDER BY ts, signal_id").fetchall()

    for row in rows:
        key = (row["strategy"], row["kind"])
        if float(row["net_edge"] or 0.0) <= 0:
            add_decision(row, status="skipped", action="skip",
                         reason="non-positive edge")
            continue

        legs = json.loads(row["legs_json"] or "[]")
        sides = {str(leg.get("side", "")).upper() for leg in legs}
        exec_sets = float(row["exec_sets"] or 0.0)
        outcome = row["outcome"]

        if sides == {"BUY"}:
            cost_per_set = sum(float(leg["price"]) for leg in legs)
            if cost_per_set <= 0:
                add_decision(row, status="skipped", action="buy", reason="bad quoted cost",
                             legs=legs)
                continue
            sets = min(exec_sets, cash / cost_per_set if cash > 0 else 0.0)
            if sets <= 1e-9:
                add_decision(row, status="skipped", action="buy", reason="no cash left",
                             cost_per_set=cost_per_set, legs=legs)
                continue
            notional = sets * cost_per_set
            cash -= notional
            deployed_notional += notional
            for leg in legs:
                token = str(leg["token_id"])
                price = float(leg["price"])
                pos = positions.setdefault(token, {
                    "token_id": token, "market_id": leg.get("market_id"),
                    "size": 0.0, "avg_price": 0.0,
                })
                old_size = pos["size"]
                new_size = old_size + sets
                pos["avg_price"] = ((old_size * pos["avg_price"]) + (sets * price)) / new_size
                pos["size"] = new_size
            reason = "cash cap" if sets < exec_sets else "quoted depth"
            sim_pnl = float(outcome) * sets if outcome is not None else 0.0
            add_decision(row, status="picked", action="buy", reason=reason,
                         sets=sets, notional=notional, cost_per_set=cost_per_set,
                         sim_pnl=sim_pnl, legs=legs)
            picked[key]["selected"] += 1
            picked[key]["notional"] += notional
            picked[key]["sim_pnl"] += sim_pnl

        elif sides == {"SELL"}:
            proceeds_per_set = sum(float(leg["price"]) for leg in legs)
            available_sets = min(
                (positions.get(str(leg["token_id"]), {}).get("size", 0.0) for leg in legs),
                default=0.0,
            )
            sets = min(exec_sets, available_sets)
            if sets <= 1e-9:
                add_decision(row, status="skipped", action="sell",
                             reason="no paper inventory to close",
                             cost_per_set=proceeds_per_set, legs=legs)
                continue
            notional = sets * proceeds_per_set
            paper_pnl = 0.0
            for leg in legs:
                token = str(leg["token_id"])
                price = float(leg["price"])
                pos = positions[token]
                paper_pnl += sets * (price - pos["avg_price"])
                pos["size"] -= sets
                if pos["size"] <= 1e-9:
                    pos["size"] = 0.0
                    pos["avg_price"] = 0.0
            cash += notional
            sold_notional += notional
            realized_pnl += paper_pnl
            reason = "inventory cap" if sets < exec_sets else "quoted depth"
            sim_pnl = float(outcome) * sets if outcome is not None else 0.0
            add_decision(row, status="picked", action="sell", reason=reason,
                         sets=sets, notional=notional, cost_per_set=proceeds_per_set,
                         paper_pnl=paper_pnl, sim_pnl=sim_pnl, legs=legs)
            picked[key]["selected"] += 1
            picked[key]["notional"] += notional
            picked[key]["paper_pnl"] += paper_pnl
            picked[key]["sim_pnl"] += sim_pnl

        else:
            add_decision(row, status="skipped", action="skip",
                         reason="mixed or unsupported leg sides", legs=legs)

    for key, values in picked.items():
        if key in strategy_rows:
            strategy_rows[key].update({
                "selected": values["selected"],
                "notional": round(values["notional"], 4),
                "paper_pnl": round(values["paper_pnl"], 4),
                "sim_pnl": round(values["sim_pnl"], 4),
            })

    open_positions = []
    open_cost = 0.0
    for pos in positions.values():
        if pos["size"] <= 1e-9:
            continue
        meta = market_info(pos["market_id"])
        cost = pos["size"] * pos["avg_price"]
        open_cost += cost
        open_positions.append({
            "token_id": pos["token_id"],
            "market_id": pos["market_id"],
            "url": meta["url"],
            "slug": meta["slug"],
            "question": meta["question"],
            "size": round(pos["size"], 4),
            "avg_price": round(pos["avg_price"], 4),
            "cost": round(cost, 4),
        })

    equity_at_cost = cash + open_cost
    return {
        "bankroll": round(bankroll, 4),
        "cash": round(cash, 4),
        "open_cost": round(open_cost, 4),
        "equity_at_cost": round(equity_at_cost, 4),
        "realized_pnl": round(realized_pnl, 4),
        "total_pnl_at_cost": round(equity_at_cost - bankroll, 4),
        "deployed_notional": round(deployed_notional, 4),
        "sold_notional": round(sold_notional, 4),
        "selected_bets": sum(v["selected"] for v in picked.values()),
        "strategy_scope": "all executable signals",
        "decisions": decisions[-PAPER_DECISION_LIMIT:],
        "strategy_selection": list(strategy_rows.values()),
        "positions": open_positions[:PAPER_DECISION_LIMIT],
        "note": "Open positions are carried at cost; no live mark-to-market is available in state.db.",
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
        "execution_intents": scalar("SELECT COUNT(*) FROM execution_intents"),
        "fills": scalar("SELECT COUNT(*) FROM execution_fills"),
        "risk_events": scalar("SELECT COUNT(*) FROM risk_events"),
        "positions": scalar("SELECT COUNT(*) FROM positions WHERE ABS(size)>0"),
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

    earnings = {
        "signal_ev": round(scalar(
            "SELECT COALESCE(SUM(net_edge * exec_sets),0) FROM signal_log"), 4),
        "labeled_sim_pnl": round(scalar(
            "SELECT COALESCE(SUM(pnl),0) FROM signal_log WHERE pnl IS NOT NULL"), 4),
        "paper_realized_pnl": round(scalar(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM positions"), 4),
        "paper_open_cost": round(scalar(
            "SELECT COALESCE(SUM(ABS(size) * avg_price),0) FROM positions WHERE ABS(size)>0"), 4),
        "filled_notional": round(scalar(
            "SELECT COALESCE(SUM(price * size),0) FROM execution_fills"), 4),
    }
    paper_portfolio = _paper_portfolio(conn, settings)

    # category breakdown
    categories = [
        {"category": (r["category"] or "(none)"), "n": r["n"]}
        for r in conn.execute(
            "SELECT category, COUNT(*) n FROM markets GROUP BY category ORDER BY n DESC LIMIT 10")
    ]

    # recent signals
    signals = [dict(r) for r in conn.execute(
        "SELECT signal_id, strategy, kind, group_id, net_edge, exec_sets, outcome, ts "
        "FROM signal_log ORDER BY ts DESC LIMIT 15")]

    # signal performance by strategy/kind (labeler outcomes)
    signal_perf = [dict(r) for r in conn.execute(
        "SELECT strategy, kind, COUNT(*) n, "
        "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) labeled, "
        "AVG(outcome) avg_outcome, "
        "COALESCE(SUM(net_edge * exec_sets),0) signal_ev, "
        "COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END),0) sim_pnl, "
        "AVG(CASE WHEN outcome > 0 THEN 1.0 WHEN outcome IS NOT NULL THEN 0.0 END) hit_rate "
        "FROM signal_log GROUP BY strategy, kind ORDER BY strategy, kind")]

    # recent execution decisions
    execution = [dict(r) for r in conn.execute(
        "SELECT intent_id, signal_id, kind, side, token_id, price, size, notional, "
        "status, reason, updated_at FROM execution_intents ORDER BY updated_at DESC LIMIT 15")]

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
        "earnings": earnings,
        "paper_portfolio": paper_portfolio,
        "categories": categories,
        "signals": signals,
        "signal_perf": signal_perf,
        "execution": execution,
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
  section h3 { font-size:12px; margin:18px 0 8px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.5px; font-weight:600; }
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
  a.ext { color:var(--accent); text-decoration:none; }
  a.ext:hover { text-decoration:underline; }
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
    <h2>Paper portfolio ($50 bankroll)</h2>
    <div id="paperportfolio"></div>
    <h3>Strategy selection</h3>
    <div id="strategyselect"></div>
    <h3>Bet sizing / paper trades</h3>
    <div id="betsizing"></div>
    <h3>Open paper positions</h3>
    <div id="paperpositions"></div>
  </section>
  <section>
    <h2>Recent signals</h2>
    <div id="signals"></div>
  </section>
  <section>
    <h2>Secondary diagnostics</h2>
    <div id="earnings"></div>
  </section>
  <section>
    <h2>Signal performance (labeled forward returns)</h2>
    <div id="signalperf"></div>
  </section>
  <section>
    <h2>Recent execution</h2>
    <div id="execution"></div>
  </section>
  <section>
    <h2>Top tracked markets by liquidity</h2>
    <div id="markets"></div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
const fmtMoney = v => v == null ? "n/a" : (Number(v) < 0 ? "-$" : "$") +
  Math.abs(Number(v)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtAge = s => s == null ? "—" : (s < 90 ? s.toFixed(0)+"s" : (s/60).toFixed(1)+"m");
const fmtUsd = v => v == null ? "—" : "$" + Math.round(v).toLocaleString();
const esc = s => {
  const span = document.createElement("span");
  span.textContent = String(s ?? "");
  return span.innerHTML;
};
const short = (s, n=18) => String(s ?? "").slice(0, n);
const extLink = (url, label, title) => url
  ? `<a class="ext" href="${esc(url)}" title="${esc(title || url)}">${esc(label)}</a>`
  : esc(label);
const tokenLinks = tokens => (tokens || []).map(t =>
  extLink(t.url, `${t.side}:${short(t.token_id, 8)}`, `${t.question || t.slug || "Polymarket market"}\n${t.token_id}`)
).join(" ");

function card(label, value, sub) {
  return `<div class="card"><div class="label">${label}</div>
    <div class="value">${value}${sub ? ` <small>${sub}</small>` : ""}</div></div>`;
}

async function refresh() {
  let s;
  try { s = await (await fetch("/api/state")).json(); }
  catch (e) { $("updated").innerHTML = '<span class="err">engine/db unreachable</span>'; return; }

  const hb = s.engine_heartbeat, el = s.event_log;
  const pp = s.paper_portfolio;
  $("cards").innerHTML =
    card("Markets", s.counts.markets.toLocaleString()) +
    card("NegRisk groups", s.counts.neg_risk_groups.toLocaleString()) +
    card("Signals fired", s.counts.signals.toLocaleString()) +
    card("Portfolio", fmtMoney(pp.bankroll)) +
    card("Paper PnL", fmtMoney(pp.total_pnl_at_cost), "at cost") +
    card("Paper cash", fmtMoney(pp.cash)) +
    card("Open cost", fmtMoney(pp.open_cost)) +
    card("Paper bets", pp.selected_bets.toLocaleString()) +
    card("Exec intents", s.counts.execution_intents.toLocaleString()) +
    card("Risk events", s.counts.risk_events.toLocaleString()) +
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

  $("paperportfolio").innerHTML =
    `<table><tr><th>metric</th><th class="num">value</th></tr>
      <tr><td>Starting bankroll</td><td class="num">${fmtMoney(pp.bankroll)}</td></tr>
      <tr><td>Paper PnL</td><td class="num">${fmtMoney(pp.total_pnl_at_cost)}</td></tr>
      <tr><td>Realized paper PnL</td><td class="num">${fmtMoney(pp.realized_pnl)}</td></tr>
      <tr><td>Cash</td><td class="num">${fmtMoney(pp.cash)}</td></tr>
      <tr><td>Open position cost</td><td class="num">${fmtMoney(pp.open_cost)}</td></tr>
      <tr><td>Equity at cost</td><td class="num">${fmtMoney(pp.equity_at_cost)}</td></tr>
      <tr><td>Deployed notional</td><td class="num">${fmtMoney(pp.deployed_notional)}</td></tr>
      <tr><td>Sold notional</td><td class="num">${fmtMoney(pp.sold_notional)}</td></tr>
      <tr><td>Strategy scope</td><td class="num">${esc(pp.strategy_scope)}</td></tr></table>
      <div class="empty">${esc(pp.note)}</div>`;

  $("strategyselect").innerHTML = pp.strategy_selection.length ?
    `<table><tr><th>strategy</th><th>kind</th><th>status</th><th class="num">signals</th>
       <th class="num">exec</th><th class="num">picked</th><th class="num">notional</th>
       <th class="num">paper PnL</th><th class="num">sim PnL</th></tr>` +
    pp.strategy_selection.map(p => `<tr><td><span class="tag">${esc(p.strategy)}</span></td>
      <td>${esc(p.kind)}</td><td>${esc(p.status)}</td>
      <td class="num">${p.signals}</td><td class="num">${p.executable}</td>
      <td class="num">${p.selected}</td><td class="num">${fmtMoney(p.notional)}</td>
      <td class="num">${fmtMoney(p.paper_pnl)}</td><td class="num">${fmtMoney(p.sim_pnl)}</td></tr>`).join("") + `</table>`
    : '<div class="empty">no strategy rows yet</div>';

  $("betsizing").innerHTML = pp.decisions.length ?
    `<table><tr><th>signal</th><th>strategy</th><th>kind</th><th>tokens</th><th>action</th><th>status</th>
       <th>reason</th><th class="num">$/set</th><th class="num">sets</th>
       <th class="num">notional</th><th class="num">edge</th><th class="num">paper PnL</th>
       <th class="num">sim PnL</th></tr>` +
    pp.decisions.map(d => `<tr><td>${d.signal_id}</td><td><span class="tag">${esc(d.strategy)}</span></td>
      <td>${esc(d.kind)}</td><td>${tokenLinks(d.tokens)}</td><td>${esc(d.action)}</td><td>${esc(d.status)}</td>
      <td>${esc(d.reason)}</td><td class="num">${fmtMoney(d.cost_per_set)}</td>
      <td class="num">${Number(d.sets).toFixed(2)}</td><td class="num">${fmtMoney(d.notional)}</td>
      <td class="num">${Number(d.net_edge).toFixed(4)}</td><td class="num">${fmtMoney(d.paper_pnl)}</td>
      <td class="num">${fmtMoney(d.sim_pnl)}</td></tr>`).join("") + `</table>`
    : '<div class="empty">no executable paper decisions yet</div>';

  $("paperpositions").innerHTML = pp.positions.length ?
    `<table><tr><th>token</th><th>market</th><th class="num">shares</th>
       <th class="num">avg price</th><th class="num">cost</th></tr>` +
    pp.positions.map(p => `<tr><td>${extLink(p.url, short(p.token_id, 18), p.question || p.slug || p.token_id)}</td>
      <td>${extLink(p.url, short(p.market_id, 18), p.question || p.slug || p.market_id)}</td>
      <td class="num">${Number(p.size).toFixed(2)}</td>
      <td class="num">${Number(p.avg_price).toFixed(4)}</td>
      <td class="num">${fmtMoney(p.cost)}</td></tr>`).join("") + `</table>`
    : '<div class="empty">no open paper positions</div>';

  $("signals").innerHTML = s.signals.length ?
    `<table><tr><th>id</th><th>strategy</th><th>kind</th><th>group</th><th class="num">net edge</th>
       <th class="num">sets</th><th class="num">outcome</th><th class="num">age</th></tr>` +
    s.signals.map(g => `<tr><td>${g.signal_id}</td><td><span class="tag">${esc(g.strategy)}</span></td>
      <td>${esc(g.kind)}</td>
      <td>${esc((g.group_id||"").slice(0,18))}…</td>
      <td class="num">${g.net_edge.toFixed(4)}</td><td class="num">${g.exec_sets.toFixed(1)}</td>
      <td class="num">${g.outcome == null ? "—" : g.outcome.toFixed(4)}</td>
      <td class="num">${fmtAge(s.now - g.ts)}</td></tr>`).join("") + `</table>`
    : '<div class="empty">no signals fired yet — expected when no arb exists (system is signal-only)</div>';

  $("earnings").innerHTML =
    `<table><tr><th>metric</th><th class="num">value</th></tr>
      <tr><td>Signal EV at full quoted depth</td><td class="num">${fmtMoney(s.earnings.signal_ev)}</td></tr>
      <tr><td>Labeler sim PnL at full quoted depth</td><td class="num">${fmtMoney(s.earnings.labeled_sim_pnl)}</td></tr>
      <tr><td>Execution-fill realized PnL</td><td class="num">${fmtMoney(s.earnings.paper_realized_pnl)}</td></tr>
      <tr><td>Execution-fill open cost</td><td class="num">${fmtMoney(s.earnings.paper_open_cost)}</td></tr>
      <tr><td>Execution-fill notional</td><td class="num">${fmtMoney(s.earnings.filled_notional)}</td></tr></table>`;

  $("signalperf").innerHTML = s.signal_perf.length ?
    `<table><tr><th>strategy</th><th>kind</th><th class="num">signals</th>
       <th class="num">labeled</th><th class="num">hit rate</th><th class="num">avg fwd edge</th>
       <th class="num">signal EV</th><th class="num">sim PnL</th></tr>` +
    s.signal_perf.map(p => `<tr><td><span class="tag">${esc(p.strategy)}</span></td>
      <td>${esc(p.kind)}</td><td class="num">${p.n}</td><td class="num">${p.labeled}</td>
      <td class="num">${p.labeled ? (p.hit_rate*100).toFixed(0)+"%" : "—"}</td>
      <td class="num">${p.labeled ? (p.avg_outcome>=0?"+":"")+p.avg_outcome.toFixed(4) : "—"}</td>
      <td class="num">${fmtMoney(p.signal_ev)}</td>
      <td class="num">${fmtMoney(p.sim_pnl)}</td>
      </tr>`).join("") + `</table>`
    : '<div class="empty">no signals to evaluate yet</div>';

  $("execution").innerHTML = s.execution.length ?
    `<table><tr><th>id</th><th>signal</th><th>kind</th><th>token</th><th>side</th>
       <th class="num">price</th><th class="num">size</th><th class="num">notional</th>
       <th>status</th><th>reason</th></tr>` +
    s.execution.map(e => `<tr><td>${e.intent_id}</td><td>${e.signal_id ?? ""}</td>
      <td>${esc(e.kind)}</td><td>${esc((e.token_id||"").slice(0,18))}</td><td>${esc(e.side)}</td>
      <td class="num">${Number(e.price).toFixed(4)}</td>
      <td class="num">${Number(e.size).toFixed(1)}</td>
      <td class="num">${fmtUsd(e.notional)}</td>
      <td>${esc(e.status)}</td><td>${esc((e.reason||"").slice(0,60))}</td></tr>`).join("") + `</table>`
    : '<div class="empty">execution disabled or no signal has passed into execution yet</div>';

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
