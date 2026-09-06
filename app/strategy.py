"""Stop-And-Reverse Martingale engine.

The cycle, unchanged from the original build:

  1. Market entry with the base lot.
  2. An opposite pending STOP at a fixed distance from price.
  3. The pending trails behind price, only in the favourable direction.
  4. When it triggers, the new position opens and both stay open (hedged).
  5. Next rung's lot: basket in LOSS -> multiplied lot; basket in PROFIT -> base lot.
  6. The whole basket closes when net profit reaches the target.

What changed is only how state arrives. The Open API pushed execution events;
MCP has no streaming, so state is polled and rebuilt each pass. That is actually
sturdier - there is no incremental bookkeeping to drift out of sync with the
broker - and it costs nothing that matters here, because the pending STOP that
opens each reversal lives at the broker and triggers server-side whether or not
this loop happens to be looking.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from app.broker import Broker
from config import Config
from utils.logging import get_logger
from utils.telegram import TelegramNotifier, esc

log = get_logger("strategy")


class Engine:
    def __init__(self, cfg: Config, broker: Broker,
                 notifier: Optional[TelegramNotifier] = None) -> None:
        self._cfg = cfg
        self.api = broker
        self._notifier = notifier

        self.running = cfg.autostart
        self.halted = False
        self.halt_reason = ""

        self.day: Optional[object] = None
        self.day_start_balance = 0.0
        self.cycles_closed = 0
        self.last_action = "—"
        self._history: deque = deque(maxlen=600)   # (timestamp, mid)

    # -- control -----------------------------------------------------------

    def start(self) -> None:
        if self.halted:
            self.halted = False
            self.halt_reason = ""
        self.running = True
        self.last_action = "started"
        log.info("Engine started.")

    def stop(self) -> None:
        self.running = False
        self.last_action = "stopped"
        log.info("Engine stopped (open positions are left alone).")

    async def panic_close(self) -> None:
        """Close everything now, profit or not."""
        self.running = False
        await self._close_all("emergency close from the panel")
        self.last_action = "EMERGENCY CLOSE"

    async def halt(self, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self.running = False
        await self._cancel_pendings()
        log.warning("HALT: %s", reason)
        await self._notify(f"🛑 <b>Halted</b>\n{esc(reason)}", key="halt")

    async def _notify(self, text: str, *, key: str = "") -> None:
        if self._notifier is not None:
            await self._notifier.send(text, dedup_key=key or None)

    # -- views over broker state ------------------------------------------

    @property
    def step(self) -> int:
        """How deep the ladder is, derived from what is actually open.

        This used to be a stored counter refreshed inside on_tick, which meant
        any other reader - the panel, a notification - could see a stale depth
        and size the next rung from it.
        """
        return max(0, len(self.positions) - 1)

    @property
    def positions(self) -> list:
        return self.api.our_positions()

    @property
    def orders(self) -> list:
        """Only pending STOP orders are part of the cycle."""
        return [o for o in self.api.our_pending()
                if "STOP" in (o.order_type or "STOP").upper()]

    def basket_pnl(self) -> float:
        """Net open P&L of the basket.

        The broker's own per-position figure is preferred when it is there: it
        already carries swap and commission, and on a martingale basket those
        are not a rounding error.
        """
        total = 0.0
        for p in self.positions:
            if p.profit is not None:
                total += p.profit
                continue
            if not p.entry_price:
                continue
            close_price = self.api.bid if p.side == "BUY" else self.api.ask
            direction = 1.0 if p.side == "BUY" else -1.0
            total += (close_price - p.entry_price) * direction * \
                p.volume * self._cfg.contract_size
        return total

    def open_lots(self) -> float:
        return sum(p.volume for p in self.positions)

    def net_lots(self) -> float:
        return sum(p.volume * (1 if p.side == "BUY" else -1) for p in self.positions)

    def next_lots(self) -> float:
        if self.basket_pnl() >= 0:
            return self._cfg.start_lots
        return self._cfg.start_lots * (self._cfg.multiplier ** (self.step + 1))

    # -- gates -------------------------------------------------------------

    def in_blackout(self) -> bool:
        cfg = self._cfg
        if not cfg.news_blackout or cfg.blackout_duration_min <= 0:
            return False
        now = datetime.now(timezone.utc)
        start = now.replace(hour=cfg.blackout_hour_utc, minute=cfg.blackout_minute_utc,
                            second=0, microsecond=0)
        minutes = (now - start).total_seconds() / 60.0
        return 0 <= minutes < cfg.blackout_duration_min

    def spread_pips(self) -> float:
        if not self.api.pip_size:
            return 0.0
        return (self.api.ask - self.api.bid) / self.api.pip_size

    def gates_open(self) -> bool:
        if self.spread_pips() > self._cfg.max_spread_pips:
            return False
        if self.in_blackout():
            return False
        return True

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self.day != today:
            self.day = today
            self.day_start_balance = self.api.balance
            self.halted = False
            self.halt_reason = ""

    def day_pnl(self) -> float:
        return self.api.balance - self.day_start_balance

    # -- the loop ----------------------------------------------------------

    async def on_tick(self) -> None:
        if not self.api.ready:
            return

        now = time.time()
        self._history.append((now, (self.api.bid + self.api.ask) / 2.0))
        self._roll_day()

        # 1) The basket target is checked ALWAYS, even while stopped, so an open
        #    cycle can still close in profit instead of hanging there.
        if self.positions:
            pnl = self.basket_pnl()
            if pnl >= self._cfg.basket_target_usd:
                await self._close_all("target reached: %.2f USD" % pnl)
                self.cycles_closed += 1
                return

        if not self.running or self.halted:
            return

        if self._cfg.daily_loss_limit_usd > 0 and \
                self.day_pnl() <= -self._cfg.daily_loss_limit_usd:
            await self.halt("daily loss limit hit: %.2f USD" % self.day_pnl())
            return

        # 2) Nothing open -> start a new cycle.
        if not self.positions:
            if self.orders:
                await self._cancel_pendings()
                return
            if not self.gates_open():
                return
            await self._open_first()
            return

        # 3) Keep the opposite pending in place.
        await self._maintain_reverse()

    def _price_ref(self) -> float:
        """The mid from ~60s ago; the current mid if we have no history yet."""
        cutoff = time.time() - 60.0
        for ts, mid in self._history:
            if ts >= cutoff:
                return mid
        return (self.api.bid + self.api.ask) / 2.0

    async def _open_first(self) -> None:
        side = self._cfg.first_entry
        if side not in ("buy", "sell"):
            # last_candle: the direction of the last ~60 seconds of movement.
            mid = (self.api.bid + self.api.ask) / 2.0
            side = "buy" if mid >= self._price_ref() else "sell"
        await self.api.market_order(side, self._cfg.start_lots, self._cfg.stop_pips)
        self.last_action = "new cycle: %s %.2f lots" % (side, self._cfg.start_lots)
        log.info(self.last_action)
        await self._notify(
            f"▶️ <b>New cycle</b>\n{esc(side.upper())} {self._cfg.start_lots} lots "
            f"{esc(self._cfg.symbol)} @ {self.api.ask if side == 'buy' else self.api.bid:.2f}")

    async def _maintain_reverse(self) -> None:
        cfg = self._cfg
        net = self.net_lots()
        if abs(net) < 1e-9:
            await self._cancel_pendings()
            return

        net_long = net > 0
        reverse_side = "sell" if net_long else "buy"

        # The two hard stops on the ladder. Neither is decoration.
        if self.step + 1 >= cfg.max_steps:
            await self._cancel_pendings()
            return
        nxt = self.next_lots()
        if self.open_lots() + nxt > cfg.max_total_lots:
            await self._cancel_pendings()
            return

        distance = cfg.reverse_pips * self.api.pip_size
        target = (self.api.bid - distance) if net_long else (self.api.ask + distance)
        target = round(target, self.api.digits)

        existing = self.orders[0] if self.orders else None

        if existing is None:
            if not self.gates_open():
                return
            await self.api.stop_order(reverse_side, nxt, target, cfg.stop_pips)
            self.last_action = "pending %s %.2f lots @ %.2f" % (reverse_side, nxt, target)
            log.info(self.last_action)
            return

        if existing.side.lower() != reverse_side:
            await self.api.cancel_order(existing.order_id)
            return

        if existing.price is None:
            return
        if abs(existing.price - target) < cfg.trail_step_pips * self.api.pip_size:
            return

        # Trail only in the favourable direction - never widen the reversal.
        should_move = (target < existing.price) if reverse_side == "buy" \
            else (target > existing.price)
        if not should_move:
            return

        await self.api.amend_stop_order(existing.order_id, nxt, target,
                                        reverse_side, cfg.stop_pips)
        self.last_action = "trailed pending to %.2f" % target

    async def _close_all(self, reason: str) -> None:
        pnl = self.basket_pnl()
        for position in list(self.positions):
            try:
                await self.api.close_position(position.position_id, position.volume)
            except Exception as exc:  # noqa: BLE001 - one failure must not strand the rest
                log.error("Could not close %s: %s", position.position_id, exc)
        await self._cancel_pendings()
        try:
            await self.api.refresh_balance()
        except Exception as exc:  # noqa: BLE001
            log.warning("Balance refresh after close failed: %s", exc)
        self.last_action = "cycle closed (%s)" % reason
        log.info(self.last_action)
        await self._notify(f"✅ <b>Cycle closed</b>\n{esc(reason)}\n"
                           f"Basket P&amp;L {pnl:+.2f} USD")

    async def _cancel_pendings(self) -> None:
        for order in list(self.orders):
            try:
                await self.api.cancel_order(order.order_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not cancel %s: %s", order.order_id, exc)

    # -- panel -------------------------------------------------------------

    def status(self) -> dict:
        cfg = self._cfg
        return {
            "mode": cfg.trading_mode,
            "live": cfg.is_live,
            "connected": self.api.connected,
            "ready": self.api.ready,
            "running": self.running,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "symbol": cfg.symbol,
            "bid": round(self.api.bid, self.api.digits),
            "ask": round(self.api.ask, self.api.digits),
            "spread_pips": round(self.spread_pips(), 1),
            "max_spread_pips": cfg.max_spread_pips,
            "balance": round(self.api.balance, 2),
            "day_pnl": round(self.day_pnl(), 2),
            "daily_limit": cfg.daily_loss_limit_usd,
            "positions": len(self.positions),
            "pendings": len(self.orders),
            "open_lots": round(self.open_lots(), 2),
            "max_lots": cfg.max_total_lots,
            "next_lots": round(self.next_lots(), 2),
            "step": self.step,
            "max_steps": cfg.max_steps,
            "basket_pnl": round(self.basket_pnl(), 2),
            "target": cfg.basket_target_usd,
            "cycles_closed": self.cycles_closed,
            "blackout": self.in_blackout(),
            "last_action": self.last_action,
            "error": self.api.last_error,
        }
