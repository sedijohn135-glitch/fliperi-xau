"""Entry point: MCP session, engine loop and the control panel, one asyncio loop.

The Twisted reactor is gone with the Open API client. Everything here is plain
asyncio: uvicorn serves the panel, a background task drives the engine, and the
MCP connection supervises itself.
"""

from __future__ import annotations

import asyncio
import signal
import sys

import uvicorn

from app import web
from app.broker import Broker
from app.strategy import Engine
from config import Config, ConfigError, load_config
from mcp_client.token_info import TokenInfo, decode_token
from mcp_client.transport import MCPConnection
from utils.logging import get_logger, register_secret, setup_logging
from utils.telegram import NullNotifier, TelegramNotifier

log = get_logger("main")

BANNER = r"""
 __  __ _   _   _   ___ _ _
 \ \/ /| \ | | | | | __| (_)_ __ _ __  ___ _ _
  >  < |  \| | | |_| _|| | | '_ \ '_ \/ -_) '_|
 /_/\_\|_|\__|  \___/  |_|_| .__/ .__/\___|_|
   Stop-And-Reverse Martingale  |_|  |_|
"""


def announce(cfg: Config, token: TokenInfo) -> None:
    """Make the account and the ladder's real size impossible to miss."""
    log.info("cTrader account %s", token.describe())
    log.info("Instrument %s | pip=%s point=%s contract=%s",
             cfg.symbol, cfg.pip_size, cfg.point_size, cfg.contract_size)
    log.info("Cycle: start %.2f lots, x%.2f, reverse %.0f pips (%.2f), "
             "stop %.0f pips (%.2f), target %.2f USD",
             cfg.start_lots, cfg.multiplier, cfg.reverse_pips,
             cfg.reverse_pips * cfg.pip_size, cfg.stop_pips,
             cfg.stop_pips * cfg.pip_size, cfg.basket_target_usd)

    # Spell out where the ladder actually ends. "Step 6" means nothing until it
    # is written as lots and dollars.
    lots, rung = [], cfg.start_lots
    for _ in range(cfg.max_steps):
        lots.append(rung)
        rung *= cfg.multiplier
    log.warning("Martingale ladder: %s = %.2f lots total if every rung fills. "
                "MAX_TOTAL_LOTS=%.2f stops it first.",
                " + ".join(f"{x:.2f}" for x in lots), sum(lots), cfg.max_total_lots)
    log.warning("At %.2f lots a 1.00 move in gold is %.2f USD.",
                cfg.max_total_lots, cfg.max_total_lots * cfg.contract_size)

    if cfg.is_live and token.is_live and not cfg.allow_live_environment:
        log.warning("=" * 76 + "\n  TRADING_MODE=live but the token points at a LIVE "
                    "account and\n  ALLOW_LIVE_ENVIRONMENT is not set. Orders stay "
                    "SIMULATED.\n" + "=" * 76)
    elif cfg.is_live and token.is_demo:
        log.info("Live order execution enabled against a DEMO account.")
    elif not cfg.is_live:
        log.info("TRADING_MODE=paper: the engine runs, no orders are sent.")


async def engine_loop(cfg: Config, engine: Engine, connection: MCPConnection) -> None:
    """Poll broker state and let the engine act, forever."""
    bootstrapped_for = -1
    while True:
        try:
            if not connection.is_ready:
                await asyncio.sleep(cfg.poll_interval_seconds)
                continue
            if bootstrapped_for != connection.stats.connects:
                await engine.api.bootstrap()
                bootstrapped_for = connection.stats.connects
                if engine.day_start_balance == 0.0:
                    engine.day_start_balance = engine.api.balance
                log.info("Broker ready | balance %.2f | %s bid %.2f ask %.2f",
                         engine.api.balance, cfg.symbol, engine.api.bid, engine.api.ask)
            else:
                await engine.api.refresh()
            await engine.on_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop outlives every error
            log.exception("Engine tick failed: %s", exc)
        await asyncio.sleep(cfg.poll_interval_seconds)


async def run() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        setup_logging("INFO")
        log.error("Configuration error: %s", exc)
        return 2

    setup_logging(cfg.log_level)
    register_secret(cfg.mcp_token)
    register_secret(cfg.telegram_bot_token)
    register_secret(cfg.panel_password)
    for line in BANNER.strip("\n").splitlines():
        log.info(line)

    token = decode_token(cfg.mcp_token)
    announce(cfg, token)

    notifier = (TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
                if cfg.telegram_bot_token and cfg.telegram_chat_id else NullNotifier())
    await notifier.start()

    connection = MCPConnection(cfg, notifier)
    broker = Broker(cfg, connection)
    engine = Engine(cfg, broker, notifier)

    web.engine = engine
    web.cfg = cfg

    await connection.start()
    loop_task = asyncio.create_task(engine_loop(cfg, engine, connection), name="engine")

    server = uvicorn.Server(uvicorn.Config(
        web.app, host="0.0.0.0", port=cfg.port, log_level="warning", access_log=False))

    stop_event = asyncio.Event()
    for name in ("SIGTERM", "SIGINT"):
        try:
            asyncio.get_running_loop().add_signal_handler(
                getattr(signal, name), stop_event.set)
        except (NotImplementedError, AttributeError):  # pragma: no cover
            pass

    serve_task = asyncio.create_task(server.serve(), name="panel")
    log.info("Panel on :%s — the engine will NOT start until you press Start.",
             cfg.port)
    try:
        await asyncio.wait({serve_task, asyncio.create_task(stop_event.wait())},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True
        loop_task.cancel()
        for task in (loop_task, serve_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await connection.stop()
        await notifier.close()
    log.info("Shutdown complete.")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":
    sys.exit(main())
