"""Safe local playground for later-phase workflows.

This writes synthetic, clearly tagged records into the local SQLite DB so the
dashboard can show what Phase 1+ signals, risk decisions, intents, fills, and
positions look like. It never contacts an exchange and never uses the live
engine bus.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402
from pm.core import db  # noqa: E402
from pm.execution.accounting import apply_fill  # noqa: E402
from pm.execution.broker import DryRunBroker  # noqa: E402
from pm.execution.models import OrderIntent, intents_from_signal  # noqa: E402
from pm.execution.risk import RiskLimits, RiskManager  # noqa: E402

PLAYGROUND_STRATEGY = "playground"
PLAY_MARKET = "PLAY_MARKET"
PLAY_GROUP = "PLAY_GROUP"
PLAY_YES = "PLAY_YES"
PLAY_NO = "PLAY_NO"


def _connect(path: Path) -> sqlite3.Connection:
    return db.connect(path)


def _insert_signal(conn: sqlite3.Connection, *, kind: str = "complement",
                   sets: float = 10.0) -> int:
    if kind == "partition_buy_all":
        group_id = PLAY_GROUP
        legs = [
            {"token_id": "PLAY_A", "market_id": "PLAY_A_MARKET", "side": "BUY", "price": 0.31, "size": 100},
            {"token_id": "PLAY_B", "market_id": "PLAY_B_MARKET", "side": "BUY", "price": 0.32, "size": 100},
            {"token_id": "PLAY_C", "market_id": "PLAY_C_MARKET", "side": "BUY", "price": 0.29, "size": 100},
        ]
        gross_edge = 0.08
        fees = 0.01
        net_edge = 0.07
    else:
        group_id = PLAY_MARKET
        legs = [
            {"token_id": PLAY_YES, "market_id": PLAY_MARKET, "side": "BUY", "price": 0.41, "size": 100},
            {"token_id": PLAY_NO, "market_id": PLAY_MARKET, "side": "BUY", "price": 0.55, "size": 100},
        ]
        gross_edge = 0.04
        fees = 0.005
        net_edge = 0.035

    return db.log_signal(
        conn,
        strategy=PLAYGROUND_STRATEGY,
        kind=kind,
        group_id=group_id,
        legs=legs,
        gross_edge=gross_edge,
        fees=fees,
        net_edge=net_edge,
        exec_sets=sets,
        features={"playground": True, "note": "synthetic local-only signal"},
    )


def _limits(settings: Settings, args: argparse.Namespace, *, scenario: str) -> RiskLimits:
    max_order = args.max_order_notional
    max_signal = args.max_signal_notional
    execution_enabled = True
    execution_mode = "dry_run"
    live_trading = False
    hard_live_gate = False
    allow_unverified = False

    if scenario == "blocked-execution":
        execution_enabled = False
    elif scenario == "live-blocked":
        execution_mode = "live"
        live_trading = True
    elif scenario == "order-limit":
        max_order = 1.0
    elif scenario == "partition-approved":
        allow_unverified = True

    return RiskLimits(
        execution_enabled=execution_enabled,
        execution_mode=execution_mode,
        live_trading=live_trading,
        hard_live_gate=hard_live_gate,
        max_order_notional=max_order,
        max_signal_notional=max_signal,
        max_open_notional=args.max_open_notional,
        max_daily_loss=args.max_daily_loss,
        max_recon_diff_for_execution=args.max_recon_diff,
        allow_unverified_negrisk=allow_unverified,
        verified_groups_path=settings.verified_groups_path,
        kill_switch_path=settings.kill_switch_path,
    )


async def scenario(conn: sqlite3.Connection, settings: Settings,
                   args: argparse.Namespace) -> int:
    kind = "partition_buy_all" if args.scenario in {"partition-rejected", "partition-approved"} else "complement"
    signal_id = _insert_signal(conn, kind=kind, sets=args.sets)
    signal = db.get_signal(conn, signal_id)
    if signal is None:
        raise RuntimeError(f"inserted signal {signal_id} could not be read back")

    risk = RiskManager(_limits(settings, args, scenario=args.scenario))
    intents = intents_from_signal(signal)
    plan = risk.check_plan(conn, signal, intents)
    if not plan.approved:
        db.log_risk_event(conn, signal_id=signal_id, code=plan.code,
                          detail=f"playground: {plan.detail}", severity="info")
        print(f"signal_id={signal_id} plan rejected: {plan.code} - {plan.detail}")
        return signal_id

    broker = DryRunBroker()
    for intent in intents:
        decision = risk.check_intent(conn, intent)
        status = "planned" if decision.approved else "rejected"
        intent_id = db.record_execution_intent(
            conn, intent.to_record(), status=status,
            reason="" if decision.approved else f"playground: {decision.detail}")
        if not decision.approved:
            db.log_risk_event(conn, signal_id=signal_id, intent_id=intent_id,
                              code=decision.code,
                              detail=f"playground: {decision.detail}", severity="info")
            print(f"intent_id={intent_id} rejected: {decision.code} - {decision.detail}")
            continue
        receipt = await broker.submit(intent)
        db.update_execution_intent(conn, intent_id, status=receipt.status,
                                   reason=f"playground: {receipt.reason}",
                                   broker_order_id=receipt.broker_order_id)
        print(f"intent_id={intent_id} dry-run submitted: {receipt.broker_order_id}")

    print(f"signal_id={signal_id} scenario={args.scenario}")
    return signal_id


def paper_fill(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.intent_id is None:
        row = conn.execute(
            "SELECT * FROM execution_intents WHERE strategy=? AND status='submitted' "
            "ORDER BY updated_at DESC LIMIT 1",
            (PLAYGROUND_STRATEGY,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM execution_intents WHERE intent_id=? AND strategy=?",
            (args.intent_id, PLAYGROUND_STRATEGY)).fetchone()

    if row is None:
        raise SystemExit("no submitted playground intent found; run `scenario approved` first")

    intent_id = int(row["intent_id"])
    price = float(args.price if args.price is not None else row["price"])
    size = float(args.size if args.size is not None else row["size"])
    fee = float(args.fee)

    db.record_fill(
        conn,
        intent_id=intent_id,
        broker_order_id=row["broker_order_id"],
        venue=row["venue"],
        market_id=row["market_id"],
        token_id=row["token_id"],
        side=row["side"],
        price=price,
        size=size,
        fee=fee,
        raw={"playground": True},
    )
    apply_fill(
        conn,
        venue=row["venue"],
        market_id=row["market_id"],
        token_id=row["token_id"],
        side=row["side"],
        price=price,
        size=size,
        fee=fee,
    )
    db.update_execution_intent(conn, intent_id, status="filled", reason="playground: paper fill")
    print(f"intent_id={intent_id} paper-filled token={row['token_id']} size={size} price={price}")
    return intent_id


def label_outcome(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.all:
        rows = conn.execute(
            "SELECT signal_id, exec_sets FROM signal_log WHERE strategy=? ORDER BY signal_id",
            (PLAYGROUND_STRATEGY,)).fetchall()
    elif args.signal_id is None:
        row = conn.execute(
            "SELECT signal_id, exec_sets FROM signal_log WHERE strategy=? "
            "ORDER BY signal_id DESC LIMIT 1",
            (PLAYGROUND_STRATEGY,)).fetchone()
        rows = [] if row is None else [row]
    else:
        row = conn.execute(
            "SELECT signal_id, exec_sets FROM signal_log WHERE strategy=? AND signal_id=?",
            (PLAYGROUND_STRATEGY, args.signal_id)).fetchone()
        rows = [] if row is None else [row]

    if not rows:
        raise SystemExit("no playground signals found; run `scenario approved` first")

    for row in rows:
        pnl = float(args.outcome) * float(row["exec_sets"] or 0.0)
        conn.execute(
            "UPDATE signal_log SET outcome=?, pnl=? WHERE signal_id=?",
            (round(float(args.outcome), 5), round(pnl, 4), row["signal_id"]))
        print(f"signal_id={row['signal_id']} outcome={args.outcome:+.4f} sim_pnl=${pnl:+.2f}")
    return len(rows)


def reset(conn: sqlite3.Connection) -> None:
    signal_rows = conn.execute(
        "SELECT signal_id FROM signal_log WHERE strategy=?", (PLAYGROUND_STRATEGY,)).fetchall()
    signal_ids = [int(r["signal_id"]) for r in signal_rows]
    intent_rows = conn.execute(
        "SELECT intent_id FROM execution_intents WHERE strategy=? OR token_id LIKE 'PLAY_%'",
        (PLAYGROUND_STRATEGY,)).fetchall()
    intent_ids = [int(r["intent_id"]) for r in intent_rows]

    if intent_ids:
        placeholders = ",".join("?" for _ in intent_ids)
        conn.execute(f"DELETE FROM execution_fills WHERE intent_id IN ({placeholders})", intent_ids)
        conn.execute(f"DELETE FROM risk_events WHERE intent_id IN ({placeholders})", intent_ids)
    conn.execute("DELETE FROM execution_fills WHERE token_id LIKE 'PLAY_%'")
    conn.execute("DELETE FROM positions WHERE token_id LIKE 'PLAY_%'")
    conn.execute("DELETE FROM execution_intents WHERE strategy=? OR token_id LIKE 'PLAY_%'",
                 (PLAYGROUND_STRATEGY,))

    if signal_ids:
        placeholders = ",".join("?" for _ in signal_ids)
        conn.execute(f"DELETE FROM risk_events WHERE signal_id IN ({placeholders})", signal_ids)
    conn.execute("DELETE FROM risk_events WHERE detail LIKE 'playground:%'")
    conn.execute("DELETE FROM signal_log WHERE strategy=?", (PLAYGROUND_STRATEGY,))
    print(f"removed playground records: signals={len(signal_ids)} intents={len(intent_ids)}")


def template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SystemExit(f"{path} already exists")
    path.write_text(
        "venue,category,price,shares,is_taker,expected_fee,tolerance,source\n"
        "polymarket,politics,0.50,100,true,1.00,0.0001,manual UI check URL or note\n",
        encoding="utf-8",
    )
    print(f"wrote {path}")


def build_parser() -> argparse.ArgumentParser:
    settings = Settings()
    parser = argparse.ArgumentParser(description="Safe local playground for later-phase workflows.")
    parser.add_argument("--db", type=Path, default=settings.db_path)
    sub = parser.add_subparsers(dest="cmd", required=True)

    scen = sub.add_parser("scenario", help="create a synthetic signal and run risk/dry-run execution")
    scen.add_argument("scenario", choices=[
        "approved", "blocked-execution", "live-blocked", "order-limit",
        "partition-rejected", "partition-approved",
    ])
    scen.add_argument("--sets", type=float, default=10.0)
    scen.add_argument("--max-order-notional", type=float, default=25.0)
    scen.add_argument("--max-signal-notional", type=float, default=100.0)
    scen.add_argument("--max-open-notional", type=float, default=250.0)
    scen.add_argument("--max-daily-loss", type=float, default=50.0)
    scen.add_argument("--max-recon-diff", type=float, default=0.01)

    fill = sub.add_parser("paper-fill", help="paper-fill the latest submitted playground intent")
    fill.add_argument("--intent-id", type=int)
    fill.add_argument("--price", type=float)
    fill.add_argument("--size", type=float)
    fill.add_argument("--fee", type=float, default=0.0)

    label = sub.add_parser("label-outcome", help="set synthetic forward outcome/PnL on playground signals")
    label.add_argument("--signal-id", type=int)
    label.add_argument("--outcome", type=float, default=0.02,
                       help="signed forward edge per set; pnl = outcome * exec_sets")
    label.add_argument("--all", action="store_true", help="apply to all playground signals")

    sub.add_parser("reset", help="delete synthetic playground records")

    tmpl = sub.add_parser("fee-audit-template", help="write a manual fee audit CSV template")
    tmpl.add_argument("--path", type=Path, default=ROOT / "data" / "g0_fee_audit.csv")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = replace(Settings(), db_path=args.db)
    conn = _connect(settings.db_path)
    if args.cmd == "scenario":
        asyncio.run(scenario(conn, settings, args))
    elif args.cmd == "paper-fill":
        paper_fill(conn, args)
    elif args.cmd == "label-outcome":
        label_outcome(conn, args)
    elif args.cmd == "reset":
        reset(conn)
    elif args.cmd == "fee-audit-template":
        template(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
