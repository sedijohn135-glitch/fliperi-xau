"""Configuration for the XAU Stop-And-Reverse Martingale bot.

Everything comes from environment variables (Railway Variables).

The connection is a **single Bearer token** for the cTrader MCP server. The
earlier Open API build needed a client id, a client secret, a refresh token, a
publicly reachable domain for the OAuth redirect and a mounted volume to keep
the tokens across redeploys. None of that exists any more: one token carries
the account, the plant and the environment, and it is read straight from the
token's own payload at start-up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

#: The one real cTrader MCP endpoint. Not configurable on purpose - a typo in an
#: env var must never be able to point live order flow at another host.
MCP_UPSTREAM = "https://mcp.ctrader.com/trading/mcp"


class ConfigError(RuntimeError):
    """The environment is not fit to start the bot."""


def _raw(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.split("#", 1)[0].strip().strip('"').strip("'")
    return value or None


def env_str(name: str, default: str = "") -> str:
    value = _raw(name)
    return default if value is None else value


def env_bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float, *, minimum: Optional[float] = None,
              maximum: Optional[float] = None) -> float:
    value = _raw(name)
    if value is None:
        return float(default)
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {parsed}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {parsed}")
    return parsed


def env_int(name: str, default: int, *, minimum: Optional[int] = None,
            maximum: Optional[int] = None) -> int:
    return int(env_float(name, default, minimum=minimum, maximum=maximum))


@dataclass(frozen=True)
class Config:
    # --- connection -------------------------------------------------------
    mcp_token: str
    mcp_upstream: str = MCP_UPSTREAM
    mcp_call_timeout: float = 15.0
    mcp_connect_timeout: float = 20.0
    reconnect_base_delay: float = 2.0
    reconnect_max_delay: float = 60.0
    auth_failure_delay: float = 300.0

    # --- safety -----------------------------------------------------------
    trading_mode: str = "paper"          # paper | live
    allow_live_environment: bool = False
    autostart: bool = False              # the engine never starts itself
    panel_password: str = ""

    # --- instrument -------------------------------------------------------
    symbol: str = "XAUUSD"
    point_size: float = 0.01             # 1 point; 1 pip = 10 points on gold
    # A "pip" here is 0.01 on gold, NOT the 0.10 used in FX convention. The
    # original client derived it as 10**-pipPosition and cTrader reports
    # pipPosition=2 for XAUUSD; the recorded session confirms it (4341.87 ->
    # 4342.15 is described as ~28 pips). Using 0.10 would make REVERSE_PIPS=30
    # a $3.00 distance instead of $0.30 - every order ten times too far away.
    # MCP's get_symbols does not expose pipPosition, so it is configured here.
    pip_size: float = 0.01
    contract_size: float = 100.0         # ounces per 1.00 lot
    volume_step: float = 0.01
    min_volume: float = 0.01
    price_scale: Optional[float] = None  # None -> auto-detect
    money_scale: Optional[float] = None
    sane_price_min: float = 100.0
    sane_price_max: float = 100_000.0

    # --- the cycle --------------------------------------------------------
    start_lots: float = 0.01
    multiplier: float = 2.0
    reverse_pips: float = 30.0
    trail_step_pips: float = 5.0
    stop_pips: float = 60.0
    basket_target_usd: float = 5.0
    first_entry: str = "last_candle"     # last_candle | buy | sell

    # --- the limits that keep this from emptying an account ---------------
    max_steps: int = 6
    max_total_lots: float = 0.64
    daily_loss_limit_usd: float = 100.0
    max_spread_pips: float = 40.0

    # --- news blackout ----------------------------------------------------
    news_blackout: bool = True
    blackout_hour_utc: int = 12
    blackout_minute_utc: int = 30
    blackout_duration_min: int = 60

    # --- ops --------------------------------------------------------------
    label: str = "XAU_SAR_MG"
    poll_interval_seconds: float = 2.0
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    log_level: str = "INFO"
    port: int = 8080

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"


def load_config() -> Config:
    token = env_str("CTRADER_MCP_TOKEN")
    if not token or token.startswith("replace-me"):
        raise ConfigError(
            "CTRADER_MCP_TOKEN is missing. It is the only credential this bot "
            "needs - the Bearer token issued for your cTrader MCP account."
        )

    cfg = Config(
        mcp_token=token,
        mcp_call_timeout=env_float("MCP_CALL_TIMEOUT_SECONDS", 15.0, minimum=1.0),
        mcp_connect_timeout=env_float("MCP_CONNECT_TIMEOUT_SECONDS", 20.0, minimum=1.0),
        trading_mode=env_str("TRADING_MODE", "paper").lower(),
        allow_live_environment=env_bool("ALLOW_LIVE_ENVIRONMENT", False),
        autostart=env_bool("AUTOSTART", False),
        panel_password=env_str("PANEL_PASSWORD"),
        symbol=env_str("SYMBOL", "XAUUSD").upper(),
        point_size=env_float("POINT_SIZE", 0.01, minimum=1e-8),
        pip_size=env_float("PIP_SIZE", 0.01, minimum=1e-8),
        contract_size=env_float("CONTRACT_SIZE", 100.0, minimum=1e-8),
        volume_step=env_float("VOLUME_STEP", 0.01, minimum=1e-8),
        min_volume=env_float("MIN_VOLUME", 0.01, minimum=0.0),
        start_lots=env_float("START_LOTS", 0.01, minimum=0.0),
        multiplier=env_float("MULTIPLIER", 2.0, minimum=1.0, maximum=10.0),
        reverse_pips=env_float("REVERSE_PIPS", 30.0, minimum=0.1),
        trail_step_pips=env_float("TRAIL_STEP_PIPS", 5.0, minimum=0.0),
        stop_pips=env_float("STOP_PIPS", 60.0, minimum=0.0),
        basket_target_usd=env_float("BASKET_TARGET_USD", 5.0),
        first_entry=env_str("FIRST_ENTRY", "last_candle").lower(),
        max_steps=env_int("MAX_STEPS", 6, minimum=1, maximum=20),
        max_total_lots=env_float("MAX_TOTAL_LOTS", 0.64, minimum=0.0),
        daily_loss_limit_usd=env_float("DAILY_LOSS_LIMIT_USD", 100.0, minimum=0.0),
        max_spread_pips=env_float("MAX_SPREAD_PIPS", 40.0, minimum=0.0),
        news_blackout=env_bool("NEWS_BLACKOUT", True),
        blackout_hour_utc=env_int("BLACKOUT_HOUR_UTC", 12, minimum=0, maximum=23),
        blackout_minute_utc=env_int("BLACKOUT_MINUTE_UTC", 30, minimum=0, maximum=59),
        blackout_duration_min=env_int("BLACKOUT_DURATION_MIN", 60, minimum=0),
        label=env_str("LABEL", "XAU_SAR_MG"),
        poll_interval_seconds=env_float("POLL_INTERVAL_SECONDS", 2.0, minimum=0.5, maximum=60.0),
        telegram_bot_token=env_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=env_str("TELEGRAM_CHAT_ID"),
        log_level=env_str("LOG_LEVEL", "INFO").upper(),
        port=env_int("PORT", 8080, minimum=1, maximum=65535),
    )

    if cfg.trading_mode not in ("paper", "live"):
        raise ConfigError(f"TRADING_MODE must be paper or live, got {cfg.trading_mode!r}")
    if not cfg.panel_password:
        raise ConfigError("PANEL_PASSWORD is required - the panel can start and stop "
                          "a martingale engine, so it is never left open.")

    # The martingale ladder must be bounded by construction, not by hope.
    worst = cfg.start_lots * sum(cfg.multiplier ** i for i in range(cfg.max_steps))
    if worst > cfg.max_total_lots:
        import logging
        logging.getLogger("config").warning(
            "MAX_STEPS=%d at multiplier %.2f from %.2f lots would reach %.2f lots "
            "in total, above MAX_TOTAL_LOTS=%.2f. The lot cap will stop the ladder "
            "first - that is intended, but check it is the cap you meant.",
            cfg.max_steps, cfg.multiplier, cfg.start_lots, worst, cfg.max_total_lots)
    return cfg
