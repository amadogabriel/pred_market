"""G0 gate validation helpers.

This script checks the Phase 0 evidence that can be verified locally:

- fresh engine heartbeat
- populated state DB
- recon diffs under threshold
- valid event-log JSONL with book/system events
- monitor stale-heartbeat alert path
- optional 7-day soak marker
- optional 20-row manual fee audit CSV

It intentionally does not run the destructive iptables reconnect drill. Run
that on the Linux VPS during the actual G0 soak window.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402
from pm.execution.fee_engine import FeeEngine  # noqa: E402


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def check_heartbeat(settings: Settings) -> CheckResult:
    path = settings.heartbeat_path
    if not path.exists():
        return CheckResult("engine heartbeat", "FAIL", f"missing {path}")
    age = time.time() - path.stat().st_mtime
    if age > settings.heartbeat_stale_after:
        return CheckResult(
            "engine heartbeat", "FAIL",
            f"stale: age={age:.1f}s limit={settings.heartbeat_stale_after}s")
    return CheckResult(
        "engine heartbeat", "OK",
        f"fresh: age={age:.1f}s mtime={_iso(path.stat().st_mtime)}")


def check_db(conn: sqlite3.Connection) -> CheckResult:
    counts = {
        "markets": conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
        "rules_versions": conn.execute("SELECT COUNT(*) FROM rules_text").fetchone()[0],
        "recon_rows": conn.execute("SELECT COUNT(*) FROM recon_log").fetchone()[0],
        "signals": conn.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0],
    }
    if counts["markets"] <= 50:
        return CheckResult("state db", "FAIL", f"too few markets: {counts}")
    components = [
        f"{r['component']}={time.time() - r['ts']:.1f}s"
        for r in conn.execute("SELECT component, ts FROM heartbeats ORDER BY component")
    ]
    return CheckResult(
        "state db", "OK",
        f"{counts}; heartbeat ages: {', '.join(components) or 'none'}")


def check_recon(conn: sqlite3.Connection, max_diff: float, recent_hours: float) -> CheckResult:
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(MAX(ABS(diff)), 0) max_diff "
        "FROM recon_log WHERE diff IS NOT NULL").fetchone()
    if row["n"] == 0:
        return CheckResult("recon diffs", "FAIL", "no comparable recon rows")
    recent_since = time.time() - recent_hours * 3600.0
    recent = conn.execute(
        "SELECT COUNT(*) n, COALESCE(MAX(ABS(diff)), 0) max_diff "
        "FROM recon_log WHERE diff IS NOT NULL AND ts >= ?",
        (recent_since,)).fetchone()
    status = "OK" if row["max_diff"] < max_diff else "FAIL"
    return CheckResult(
        "recon diffs", status,
        f"all_rows={row['n']} max_abs={row['max_diff']:.4f}; "
        f"recent_{recent_hours:g}h_rows={recent['n']} recent_max_abs={recent['max_diff']:.4f}; "
        f"limit=<{max_diff:.4f}")


def _event_files(events_dir: Path) -> list[Path]:
    return sorted(events_dir.glob("*/events.jsonl"))


def check_event_logs(events_dir: Path, max_event_age: float) -> CheckResult:
    files = _event_files(events_dir)
    if not files:
        return CheckResult("event logs", "FAIL", f"no events.jsonl files under {events_dir}")

    total_lines = 0
    topics: set[str] = set()
    newest = max(files, key=lambda p: p.stat().st_mtime)

    try:
        for path in files:
            with path.open("r", encoding="utf-8") as fh:
                file_lines = 0
                for line_no, line in enumerate(fh, 1):
                    if not line.strip():
                        raise ValueError(f"{path}:{line_no}: blank line")
                    rec = json.loads(line)
                    if not {"ts", "topic", "payload"} <= set(rec):
                        raise ValueError(f"{path}:{line_no}: missing ts/topic/payload")
                    topics.add(str(rec["topic"]))
                    file_lines += 1
                if file_lines == 0:
                    raise ValueError(f"{path}: empty event log")
                total_lines += file_lines
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return CheckResult("event logs", "FAIL", str(exc))

    age = time.time() - newest.stat().st_mtime
    missing = {"book", "system"} - topics
    if missing:
        return CheckResult("event logs", "FAIL", f"missing required topics: {sorted(missing)}")
    if age > max_event_age:
        return CheckResult(
            "event logs", "FAIL",
            f"newest log not growing: {newest} age={age:.1f}s limit={max_event_age}s")
    return CheckResult(
        "event logs", "OK",
        f"{len(files)} day files, {total_lines} lines, topics={sorted(topics)}, newest_age={age:.1f}s")


def check_monitor_stale_alert(timeout_s: float) -> CheckResult:
    with tempfile.TemporaryDirectory(prefix="pm-g0-monitor-") as td:
        base = Path(td)
        db_path = base / "state.db"
        heartbeat = base / "heartbeat"
        heartbeat.write_text("old\n", encoding="utf-8")
        old = time.time() - 10.0
        os.utime(heartbeat, (old, old))

        env = os.environ.copy()
        env.update({
            "PM_DB_PATH": str(db_path),
            "PM_HEARTBEAT": str(heartbeat),
            "PM_EVENTS_DIR": str(base / "events"),
            "PM_HB_STALE": "1",
            "PM_TG_TOKEN": "",
            "PM_TG_CHAT": "",
        })
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "monitor.py")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        try:
            time.sleep(timeout_s)
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)

    combined = f"{stdout}\n{stderr}"
    if "engine heartbeat stale" not in combined:
        return CheckResult("monitor stale alert", "FAIL", combined.strip() or "no alert output")
    return CheckResult("monitor stale alert", "OK", "stale heartbeat alert printed in dev fallback mode")


def check_soak_marker(marker: Path, required_hours: float, start_soak: bool) -> CheckResult:
    if start_soak and not marker.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "started_at_utc": _utc_now().isoformat(timespec="seconds"),
            "required_hours": required_hours,
        }, indent=2) + "\n", encoding="utf-8")

    if not marker.exists():
        return CheckResult(
            "7-day soak", "BLOCKED",
            f"no soak marker; run scripts/g0_gate.py --start-soak when the soak begins")

    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
        started = datetime.fromisoformat(raw["started_at_utc"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return CheckResult("7-day soak", "FAIL", f"bad marker {marker}: {exc}")
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed_hours = (_utc_now() - started).total_seconds() / 3600.0
    if elapsed_hours >= required_hours:
        return CheckResult("7-day soak", "OK", f"elapsed={elapsed_hours:.2f}h required={required_hours:g}h")
    return CheckResult(
        "7-day soak", "BLOCKED",
        f"elapsed={elapsed_hours:.2f}h required={required_hours:g}h started={started.isoformat()}")


def check_fee_audit(csv_path: Path, fees_path: Path) -> CheckResult:
    if not csv_path.exists():
        return CheckResult(
            "20-trade fee audit", "BLOCKED",
            f"missing {csv_path}; provide >=20 manual UI checks")

    engine = FeeEngine.from_yaml(fees_path)
    failures: list[str] = []
    rows = 0
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for idx, row in enumerate(reader, 2):
            rows += 1
            try:
                venue = row["venue"]
                category = row.get("category") or None
                price = float(row["price"])
                shares = float(row["shares"])
                expected = float(row["expected_fee"])
                is_taker = row.get("is_taker", "true").strip().lower() not in {"0", "false", "no"}
                tolerance = float(row.get("tolerance") or "0.0001")
                got = engine.fee(venue, category, price, shares, is_taker=is_taker)
            except (KeyError, TypeError, ValueError) as exc:
                failures.append(f"line {idx}: bad row: {exc}")
                continue
            if abs(got - expected) > tolerance:
                failures.append(
                    f"line {idx}: expected={expected:.6f} got={got:.6f} tolerance={tolerance:.6f}")

    if rows < 20:
        return CheckResult("20-trade fee audit", "BLOCKED", f"only {rows} rows; need >=20")
    if failures:
        return CheckResult("20-trade fee audit", "FAIL", "; ".join(failures[:5]))
    return CheckResult("20-trade fee audit", "OK", f"{rows} manual fee rows matched fee engine")


def reconnect_drill_status() -> CheckResult:
    return CheckResult(
        "WS reconnect drill", "BLOCKED",
        "requires a Linux VPS/network-maintenance window; do not run iptables from this Windows desktop")


def print_results(results: list[CheckResult]) -> None:
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.status:7} {r.name:<{width}}  {r.detail}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    settings = Settings()
    parser = argparse.ArgumentParser(description="Run local G0 gate validation checks.")
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--events-dir", type=Path, default=settings.events_dir)
    parser.add_argument("--heartbeat", type=Path, default=settings.heartbeat_path)
    parser.add_argument("--fees", type=Path, default=settings.fees_yaml)
    parser.add_argument("--fee-audit-csv", type=Path, default=ROOT / "data" / "g0_fee_audit.csv")
    parser.add_argument("--soak-marker", type=Path, default=ROOT / "data" / "g0_soak_start.json")
    parser.add_argument("--start-soak", action="store_true")
    parser.add_argument("--required-soak-hours", type=float, default=168.0)
    parser.add_argument("--max-recon-diff", type=float, default=0.01)
    parser.add_argument("--recent-hours", type=float, default=1.0)
    parser.add_argument("--max-event-age", type=float, default=120.0)
    parser.add_argument("--monitor-timeout", type=float, default=4.0)
    parser.add_argument("--strict", action="store_true", help="Treat BLOCKED/WARN/SKIP as failing.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    settings = replace(
        Settings(),
        db_path=args.db,
        events_dir=args.events_dir,
        heartbeat_path=args.heartbeat,
        fees_yaml=args.fees,
    )

    results: list[CheckResult] = []
    results.append(check_heartbeat(settings))

    if args.db.exists():
        try:
            with _connect_ro(args.db) as conn:
                results.append(check_db(conn))
                results.append(check_recon(conn, args.max_recon_diff, args.recent_hours))
        except sqlite3.Error as exc:
            results.append(CheckResult("state db", "FAIL", str(exc)))
    else:
        results.append(CheckResult("state db", "FAIL", f"missing {args.db}"))
        results.append(CheckResult("recon diffs", "FAIL", "state DB missing"))

    results.append(check_event_logs(args.events_dir, args.max_event_age))
    results.append(check_monitor_stale_alert(args.monitor_timeout))
    results.append(check_soak_marker(args.soak_marker, args.required_soak_hours, args.start_soak))
    results.append(check_fee_audit(args.fee_audit_csv, args.fees))
    results.append(reconnect_drill_status())

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        print_results(results)

    hard_fail = any(r.status == "FAIL" for r in results)
    strict_fail = args.strict and any(r.status != "OK" for r in results)
    return 1 if hard_fail or strict_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
