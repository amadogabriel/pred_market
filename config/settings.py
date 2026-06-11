"""Central settings. Everything overridable via environment variables.

Secrets (Telegram token, future API keys) come ONLY from the environment /
an .env file loaded by systemd (EnvironmentFile=) — never hardcoded here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # --- paths ---
    db_path: Path = field(default_factory=lambda: Path(_env("PM_DB_PATH", str(ROOT / "data" / "state.db"))))
    events_dir: Path = field(default_factory=lambda: Path(_env("PM_EVENTS_DIR", str(ROOT / "data" / "events"))))
    heartbeat_path: Path = field(default_factory=lambda: Path(_env("PM_HEARTBEAT", str(ROOT / "data" / "heartbeat"))))
    fees_yaml: Path = field(default_factory=lambda: Path(_env("PM_FEES_YAML", str(ROOT / "config" / "fees.yaml"))))

    # --- polymarket endpoints ---
    pm_ws_url: str = field(default_factory=lambda: _env(
        "PM_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"))
    pm_clob_rest: str = field(default_factory=lambda: _env("PM_CLOB_REST", "https://clob.polymarket.com"))
    pm_gamma_rest: str = field(default_factory=lambda: _env("PM_GAMMA_REST", "https://gamma-api.polymarket.com"))

    # --- market universe ---
    track_top_n: int = field(default_factory=lambda: int(_env("PM_TRACK_TOP_N", "150")))
    min_liquidity_usd: float = field(default_factory=lambda: float(_env("PM_MIN_LIQUIDITY", "1000")))
    ws_assets_per_conn: int = field(default_factory=lambda: int(_env("PM_WS_CHUNK", "100")))

    # --- cadence (seconds) ---
    metadata_sync_interval: int = field(default_factory=lambda: int(_env("PM_META_INTERVAL", "3600")))
    recon_interval: int = field(default_factory=lambda: int(_env("PM_RECON_INTERVAL", "300")))
    heartbeat_interval: int = field(default_factory=lambda: int(_env("PM_HB_INTERVAL", "15")))
    heartbeat_stale_after: int = field(default_factory=lambda: int(_env("PM_HB_STALE", "120")))
    scan_interval: float = field(default_factory=lambda: float(_env("PM_SCAN_INTERVAL", "1.0")))
    stale_book_after: int = field(default_factory=lambda: int(_env("PM_STALE_BOOK", "30")))

    # --- struct arb (signal-only in Phase 0/1) ---
    arb_buffer: float = field(default_factory=lambda: float(_env("PM_ARB_BUFFER", "0.01")))  # 1% safety buffer
    arb_min_set_size: float = field(default_factory=lambda: float(_env("PM_ARB_MIN_SETS", "10")))  # min executable sets

    # --- telegram (monitor process) ---
    telegram_token: str = field(default_factory=lambda: _env("PM_TG_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("PM_TG_CHAT", ""))

    # --- dashboard (read-only web UI) ---
    dashboard_host: str = field(default_factory=lambda: _env("PM_DASH_HOST", "127.0.0.1"))
    dashboard_port: int = field(default_factory=lambda: int(_env("PM_DASH_PORT", "8787")))


settings = Settings()
