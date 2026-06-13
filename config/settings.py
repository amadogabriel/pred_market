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


def _env_bool(name: str, default: str = "false") -> bool:
    return _env(name, default).strip().lower() in {"1", "true", "yes", "on"}


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

    # --- execution / risk gates (Phase 1+, default closed) ---
    execution_enabled: bool = field(default_factory=lambda: _env_bool("PM_EXECUTION_ENABLED", "false"))
    execution_mode: str = field(default_factory=lambda: _env("PM_EXECUTION_MODE", "dry_run"))  # dry_run | live
    live_trading: bool = field(default_factory=lambda: _env_bool("PM_LIVE_TRADING", "false"))
    max_order_notional: float = field(default_factory=lambda: float(_env("PM_MAX_ORDER_NOTIONAL", "25")))
    max_signal_notional: float = field(default_factory=lambda: float(_env("PM_MAX_SIGNAL_NOTIONAL", "100")))
    max_open_notional: float = field(default_factory=lambda: float(_env("PM_MAX_OPEN_NOTIONAL", "250")))
    max_daily_loss: float = field(default_factory=lambda: float(_env("PM_MAX_DAILY_LOSS", "50")))
    max_recon_diff_for_execution: float = field(default_factory=lambda: float(_env("PM_MAX_RECON_DIFF_EXEC", "0.01")))
    allow_unverified_negrisk: bool = field(default_factory=lambda: _env_bool("PM_ALLOW_UNVERIFIED_NEGRISK", "false"))
    verified_groups_path: Path = field(default_factory=lambda: Path(
        _env("PM_VERIFIED_GROUPS", str(ROOT / "config" / "verified_negrisk_groups.txt"))))
    kill_switch_path: Path = field(default_factory=lambda: Path(
        _env("PM_KILL_SWITCH", str(ROOT / "data" / "KILL_SWITCH"))))

    # --- research signals (S2 microstructure / S3 relative value; never executable) ---
    research_signals_enabled: bool = field(default_factory=lambda: _env_bool("PM_RESEARCH_SIGNALS", "true"))
    micro_window_s: float = field(default_factory=lambda: float(_env("PM_MICRO_WINDOW", "300")))
    micro_min_samples: int = field(default_factory=lambda: int(_env("PM_MICRO_MIN_SAMPLES", "20")))
    micro_ofi_threshold: float = field(default_factory=lambda: float(_env("PM_MICRO_OFI", "0.6")))
    micro_max_spread: float = field(default_factory=lambda: float(_env("PM_MICRO_MAX_SPREAD", "0.03")))
    micro_liq_spread_mult: float = field(default_factory=lambda: float(_env("PM_MICRO_LIQ_SPREAD_MULT", "3.0")))
    micro_liq_depth_drop: float = field(default_factory=lambda: float(_env("PM_MICRO_LIQ_DEPTH_DROP", "0.5")))
    micro_trade_abs_floor: float = field(default_factory=lambda: float(_env("PM_MICRO_TRADE_FLOOR", "0.01")))
    micro_debounce_s: float = field(default_factory=lambda: float(_env("PM_MICRO_DEBOUNCE", "120")))
    rv_window_s: float = field(default_factory=lambda: float(_env("PM_RV_WINDOW", "1800")))
    rv_min_samples: int = field(default_factory=lambda: int(_env("PM_RV_MIN_SAMPLES", "30")))
    rv_z_threshold: float = field(default_factory=lambda: float(_env("PM_RV_Z", "3.0")))
    rv_min_abs_dev: float = field(default_factory=lambda: float(_env("PM_RV_MIN_DEV", "0.02")))
    rv_debounce_s: float = field(default_factory=lambda: float(_env("PM_RV_DEBOUNCE", "120")))

    # --- momentum / boundary-overshoot (S4 research; never executable) ---
    mom_window_s: float = field(default_factory=lambda: float(_env("PM_MOM_WINDOW", "300")))
    mom_min_samples: int = field(default_factory=lambda: int(_env("PM_MOM_MIN_SAMPLES", "20")))
    mom_z_threshold: float = field(default_factory=lambda: float(_env("PM_MOM_Z", "2.5")))
    mom_min_abs_drift: float = field(default_factory=lambda: float(_env("PM_MOM_MIN_DRIFT", "0.01")))
    mom_boundary_low: float = field(default_factory=lambda: float(_env("PM_MOM_BND_LOW", "0.05")))
    mom_boundary_high: float = field(default_factory=lambda: float(_env("PM_MOM_BND_HIGH", "0.95")))
    mom_boundary_bounce: float = field(default_factory=lambda: float(_env("PM_MOM_BND_BOUNCE", "0.01")))
    mom_debounce_s: float = field(default_factory=lambda: float(_env("PM_MOM_DEBOUNCE", "180")))

    # --- signal outcome labeler (fills signal_log.outcome with forward returns) ---
    label_horizon_s: float = field(default_factory=lambda: float(_env("PM_LABEL_HORIZON", "900")))
    label_max_age_s: float = field(default_factory=lambda: float(_env("PM_LABEL_MAX_AGE", "86400")))
    label_batch: int = field(default_factory=lambda: int(_env("PM_LABEL_BATCH", "200")))
    label_poll_s: float = field(default_factory=lambda: float(_env("PM_LABEL_POLL", "60")))

    # --- execution strategy allowlist (research strategies must never appear here) ---
    execution_strategies: frozenset[str] = field(default_factory=lambda: frozenset(
        s.strip() for s in _env("PM_EXECUTION_STRATEGIES", "struct_arb").split(",") if s.strip()))

    # --- whale-follow (S5 research; on-chain Polygon listener; never executable) ---
    polygon_rpc_url: str = field(default_factory=lambda: _env("PM_POLYGON_RPC_URL", ""))
    polygon_ctf_address: str = field(default_factory=lambda: _env(
        "PM_POLYGON_CTF_ADDRESS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"))
    polygon_poll_s: float = field(default_factory=lambda: float(_env("PM_POLYGON_POLL_S", "5")))
    polygon_lookback_blocks: int = field(default_factory=lambda: int(_env("PM_POLYGON_LOOKBACK_BLOCKS", "50")))
    whale_min_calibration: float = field(default_factory=lambda: float(_env("PM_WHALE_MIN_CALIB", "0.55")))
    whale_min_resolved: int = field(default_factory=lambda: int(_env("PM_WHALE_MIN_RESOLVED", "5")))
    whale_min_value_raw: int = field(default_factory=lambda: int(_env("PM_WHALE_MIN_VALUE_RAW", "100000000")))
    whale_debounce_s: float = field(default_factory=lambda: float(_env("PM_WHALE_DEBOUNCE", "60")))

    # --- news (S6 research; RSS poller + headline matcher; never executable) ---
    news_feeds_yaml: Path = field(default_factory=lambda: Path(_env(
        "PM_NEWS_FEEDS_YAML", str(ROOT / "config" / "news_feeds.yaml"))))
    news_min_overlap: int = field(default_factory=lambda: int(_env("PM_NEWS_MIN_OVERLAP", "2")))
    news_top_k: int = field(default_factory=lambda: int(_env("PM_NEWS_TOP_K", "3")))
    news_debounce_s: float = field(default_factory=lambda: float(_env("PM_NEWS_DEBOUNCE", "300")))
    news_index_refresh_s: float = field(default_factory=lambda: float(_env("PM_NEWS_INDEX_REFRESH", "300")))

    # --- calibration model (S7 research; periodic divergence; never executable) ---
    base_rates_yaml: Path = field(default_factory=lambda: Path(_env(
        "PM_BASE_RATES_YAML", str(ROOT / "config" / "base_rates.yaml"))))
    calibration_edge_threshold: float = field(default_factory=lambda: float(_env("PM_CALIB_EDGE", "0.10")))
    calibration_min_ttm_s: float = field(default_factory=lambda: float(_env("PM_CALIB_MIN_TTM", "86400")))
    calibration_poll_s: float = field(default_factory=lambda: float(_env("PM_CALIB_POLL", "600")))
    calibration_debounce_s: float = field(default_factory=lambda: float(_env("PM_CALIB_DEBOUNCE", "3600")))
    calibration_use_metaculus: bool = field(default_factory=lambda: _env_bool("PM_CALIB_METACULUS", "false"))

    # --- Kelly sizing (applied when an executable signal converts to an intent) ---
    kelly_factor: float = field(default_factory=lambda: float(_env("PM_KELLY_FACTOR", "0.5")))
    kelly_per_trade_cap: float = field(default_factory=lambda: float(_env("PM_KELLY_PER_TRADE_CAP", "25")))
    kelly_ttm_cliff_s: float = field(default_factory=lambda: float(_env("PM_KELLY_TTM_CLIFF", "259200")))
    kelly_ttm_floor: float = field(default_factory=lambda: float(_env("PM_KELLY_TTM_FLOOR", "0.25")))

    # --- telegram (monitor process) ---
    telegram_token: str = field(default_factory=lambda: _env("PM_TG_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("PM_TG_CHAT", ""))

    # --- dashboard (read-only web UI) ---
    dashboard_host: str = field(default_factory=lambda: _env("PM_DASH_HOST", "127.0.0.1"))
    dashboard_port: int = field(default_factory=lambda: int(_env("PM_DASH_PORT", "8787")))
    paper_portfolio_usd: float = field(default_factory=lambda: float(_env("PM_PAPER_PORTFOLIO_USD", "50")))


settings = Settings()
