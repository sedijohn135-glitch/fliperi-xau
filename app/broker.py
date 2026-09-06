"""Broker access over the cTrader MCP server.

This replaces the Open API client entirely: no protobuf, no Twisted reactor, no
OAuth dance, no token store on a mounted volume. One Bearer token, HTTP calls,
and the tool schemas read live from the server at start-up.

The wire conventions below were verified against the running server, not
assumed - each of them was a real bug first:

* ``volume`` is declared **integer** and carries cTrader centi-units
  (lots x contract_size x 100). Sent as lots it rounds to zero and every order
  is rejected.
* ``stopLoss``/``takeProfit`` are **absolute prices**; the distance variants are
  the separate ``relativeStopLoss``/``relativeTakeProfit`` fields.
* Prices arrive as integers scaled by 1e5, money in cents.
* Order and position ids are integers.

There is no streaming: state is polled. That is fine for this strategy, because
the pending STOP that opens each reversal lives at the broker and triggers
server-side whether or not the bot is watching.
"""

from __future__ import annotations

from typing import Any, Optional

from config import Config
from mcp_client.errors import ToolCallError
from mcp_client.parsing import (
    as_float, as_int, collect_numbers, extract_payload, find_list,
    normalize_side, parse_timestamp, pick,
)
from mcp_client.schema import (
    Candidates, bind_arguments, declared_types, map_properties, normalize,
)
from mcp_client.transport import MCPConnection
from models import PendingOrder, Position, SymbolInfo
from utils.logging import get_logger
from utils.prices import (
    Scale, normalize_price, resolve_money_scale, resolve_price_scale, round_to_step,
)

log = get_logger("broker")

T_BALANCE = ("get_balance", "getBalance", "get_account_info")
T_SYMBOLS = ("get_symbols", "getSymbols", "list_symbols")
T_SPOT = ("get_spot_prices", "get_spot_price", "get_quotes")
T_POSITIONS = ("get_positions", "getPositions", "list_positions")
T_PENDING = ("get_pending_orders", "getPendingOrders", "get_orders")
T_CREATE = ("create_order", "createOrder", "place_order", "new_order")
T_CANCEL = ("cancel_order", "cancelOrder", "delete_order")
T_AMEND = ("amend_order", "amendOrder", "modify_order")
T_CLOSE = ("close_position", "closePosition")

SIDE = {
    "buy": Candidates.of("BUY", "Buy", "buy", "LONG", 1),
    "sell": Candidates.of("SELL", "Sell", "sell", "SHORT", 2),
}
ORDER_TYPE = {
    "MARKET": Candidates.of("MARKET", "Market", "market", 1),
    "STOP": Candidates.of("STOP", "Stop", "stop", 3),
}


class Broker:
    """Everything the strategy needs from the account, in human units."""

    def __init__(self, cfg: Config, connection: MCPConnection) -> None:
        self._cfg = cfg
        self._conn = connection

        self.symbol: Optional[SymbolInfo] = None
        self.price_scale = Scale(1.0, "fallback")
        self.money_scale = Scale(1.0, "fallback")
        self.point_size = cfg.point_size
        self.pip_size = cfg.pip_size

        self.bid = 0.0
        self.ask = 0.0
        self.balance = 0.0
        self.positions: list[Position] = []
        self.pending: list[PendingOrder] = []
        self.last_error = ""
        self._warned: set[str] = set()

    # -- plumbing ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._conn.is_ready

    @property
    def ready(self) -> bool:
        return bool(self.connected and self.symbol and self.bid > 0 and self.ask > 0)

    @property
    def symbol_id(self) -> Optional[int]:
        return self.symbol.symbol_id if self.symbol else None

    @property
    def digits(self) -> int:
        text = f"{self.point_size:.10f}".rstrip("0")
        return len(text.split(".")[1]) if "." in text else 0

    async def _call(self, aliases, values: dict[str, Any], *, strict: bool = True) -> Any:
        tool = self._conn.catalog.require(*aliases)
        args = bind_arguments(tool, values, strict=strict)
        log.debug("call %s %s", tool.name, args)
        return extract_payload(await self._conn.call_tool(tool.name, args))

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    # -- volume ------------------------------------------------------------

    def _volume_property(self):
        tool = self._conn.catalog.find(*T_CREATE)
        if tool is None:
            return None, {}
        for prop, canonical in map_properties(tool).items():
            if canonical == "volume":
                return prop, tool.properties.get(prop, {})
        return None, {}

    def lots_to_wire(self, lots: float) -> float:
        """cTrader carries volume as 1/100 of a unit when the field is an integer."""
        prop, schema = self._volume_property()
        if prop is not None and "integer" in declared_types(schema):
            self._warn_once(
                "volume-mode",
                "create_order.%s is an integer field: volume is sent as centi-units "
                "(lots x contract_size x 100).", prop)
            return lots * self._cfg.contract_size * 100.0
        return lots

    def wire_to_lots(self, wire: float) -> float:
        prop, schema = self._volume_property()
        if prop is not None and "integer" in declared_types(schema):
            return wire / (self._cfg.contract_size * 100.0)
        return wire

    def round_lots(self, lots: float) -> float:
        return max(round_to_step(lots, self._cfg.volume_step, mode="down"),
                   self._cfg.min_volume)

    # -- start-up ----------------------------------------------------------

    async def bootstrap(self) -> None:
        self.symbol = await self._resolve_symbol(self._cfg.symbol)
        if self.symbol.digits:
            self.point_size = 10.0 ** (-self.symbol.digits)
        log.info("Instrument %s (id %s) | point=%s pip=%s contract=%s",
                 self.symbol.name, self.symbol.symbol_id, self.point_size,
                 self.pip_size, self._cfg.contract_size)
        await self._detect_scales()
        await self.refresh()

    async def _resolve_symbol(self, name: str) -> SymbolInfo:
        payload = await self._call(T_SYMBOLS, {})
        target = normalize(name)
        for record in find_list(payload, "symbols", "data"):
            symbol_name = str(pick(record, "symbolName", "name", "symbol", default=""))
            if normalize(symbol_name) == target:
                return SymbolInfo(
                    symbol_id=as_int(pick(record, "symbolId", "id"), -1) or -1,
                    name=symbol_name,
                    description=str(pick(record, "description", default="")),
                    digits=as_int(pick(record, "digits", "pipPosition"), None),
                    raw=record if isinstance(record, dict) else {},
                )
        raise ToolCallError(f"Symbol {name!r} is not available on this account.")

    async def _detect_scales(self) -> None:
        payload = await self._raw_spot()
        samples = collect_numbers(find_list(payload, "prices", "spots"),
                                  ("bid", "ask"), limit=4)
        self.price_scale = resolve_price_scale(
            self._cfg.price_scale, samples,
            self._cfg.sane_price_min, self._cfg.sane_price_max)
        log.info("Price scale: divide by %s (%s)",
                 self.price_scale.divisor, self.price_scale.source)

        payload = await self._call(T_BALANCE, {})
        record = payload if isinstance(payload, dict) else (find_list(payload) or [{}])[0]
        money = [v for v in (as_float(pick(record, "balance"), None),
                             as_float(pick(record, "equity"), None)) if v]
        self.money_scale = resolve_money_scale(self._cfg.money_scale, money)
        log.info("Money scale: divide by %s (%s)",
                 self.money_scale.divisor, self.money_scale.source)

    # -- state -------------------------------------------------------------

    async def _raw_spot(self) -> Any:
        values: dict[str, Any] = {}
        if self.symbol is not None:
            values["symbol_id"] = self.symbol.symbol_id
            values["symbol_name"] = self.symbol.name
        return await self._call(T_SPOT, values)

    async def refresh(self) -> None:
        """One full state read: quote, balance, positions, pending orders."""
        try:
            await self._refresh_quote()
            await self.refresh_balance()
            self.positions = await self._read_positions()
            self.pending = await self._read_pending()
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            self.last_error = str(exc)[:300]
            raise

    async def _refresh_quote(self) -> None:
        payload = await self._raw_spot()
        for record in find_list(payload, "prices", "spots"):
            if self.symbol_id is not None:
                found = as_int(pick(record, "symbolId", "id"), None)
                if found is not None and found != self.symbol_id:
                    continue
            bid = self.price_scale.apply(as_float(pick(record, "bid", "bidPrice"), None))
            ask = self.price_scale.apply(as_float(pick(record, "ask", "askPrice"), None))
            if bid and ask:
                self.bid, self.ask = bid, ask
                return
        raise ToolCallError(f"No spot price for {self._cfg.symbol}")

    async def refresh_balance(self) -> None:
        payload = await self._call(T_BALANCE, {})
        record = payload if isinstance(payload, dict) else (find_list(payload) or [{}])[0]
        balance = self.money_scale.apply(
            as_float(pick(record, "balance", "accountBalance"), None))
        if balance is None:
            raise ToolCallError(f"Balance payload had no balance field: {record}")
        self.balance = balance

    def _account_price(self, raw: Optional[float]) -> Optional[float]:
        """A price read back from a position or an order.

        The server is not consistent about scaling: spot prices and trendbars
        come as integers x1e5, but positions come already in human units.
        Dividing those again turned a 79,851.50 entry into 0.80 - and for this
        bot that is not cosmetic, because basket_pnl() falls back to entry
        prices whenever the broker does not report a per-position profit.
        """
        return normalize_price(raw, self.price_scale,
                               self._cfg.sane_price_min, self._cfg.sane_price_max)

    def _is_ours(self, label: str) -> bool:
        return bool(label) and self._cfg.label in label

    async def _read_positions(self) -> list[Position]:
        payload = await self._call(T_POSITIONS, {}, strict=False)
        out = []
        for record in find_list(payload, "positions", "data"):
            symbol_id = as_int(pick(record, "symbolId"), None)
            if self.symbol_id is not None and symbol_id not in (None, self.symbol_id):
                continue
            out.append(Position(
                position_id=str(pick(record, "positionId", "id", default="")),
                symbol_id=symbol_id,
                symbol_name=str(pick(record, "symbolName", "symbol", default="")),
                side=normalize_side(pick(record, "tradeSide", "side", "direction")),
                volume=self.wire_to_lots(as_float(pick(record, "volume", "lots"), 0.0) or 0.0),
                entry_price=self._account_price(
                    as_float(pick(record, "entryPrice", "openPrice", "price"), None)),
                stop_loss=self._account_price(as_float(pick(record, "stopLoss"), None)),
                take_profit=self._account_price(as_float(pick(record, "takeProfit"), None)),
                label=str(pick(record, "label", "comment", default="")),
                open_time=parse_timestamp(pick(record, "openTimestamp", "openTime")),
                profit=self.money_scale.apply(
                    as_float(pick(record, "profit", "netProfit", "grossProfit"), None)),
                raw=record if isinstance(record, dict) else {},
            ))
        return out

    async def _read_pending(self) -> list[PendingOrder]:
        payload = await self._call(T_PENDING, {}, strict=False)
        out = []
        for record in find_list(payload, "orders", "pendingOrders", "data"):
            symbol_id = as_int(pick(record, "symbolId"), None)
            if self.symbol_id is not None and symbol_id not in (None, self.symbol_id):
                continue
            out.append(PendingOrder(
                order_id=str(pick(record, "orderId", "id", default="")),
                symbol_id=symbol_id,
                symbol_name=str(pick(record, "symbolName", "symbol", default="")),
                side=normalize_side(pick(record, "tradeSide", "side", "direction")),
                volume=self.wire_to_lots(as_float(pick(record, "volume", "lots"), 0.0) or 0.0),
                price=self._account_price(
                    as_float(pick(record, "stopPrice", "limitPrice", "price"), None)),
                order_type=str(pick(record, "orderType", "type", default="")),
                stop_loss=self._account_price(as_float(pick(record, "stopLoss"), None)),
                label=str(pick(record, "label", "comment", default="")),
                raw=record if isinstance(record, dict) else {},
            ))
        return out

    def our_positions(self) -> list[Position]:
        return [p for p in self.positions if self._is_ours(p.label)] or self.positions

    def our_pending(self) -> list[PendingOrder]:
        return [o for o in self.pending if self._is_ours(o.label)] or self.pending

    # -- orders ------------------------------------------------------------

    def _sl_for(self, side: str, reference: float, stop_pips: float) -> Optional[float]:
        if stop_pips <= 0:
            return None
        distance = stop_pips * self.pip_size
        price = reference - distance if side == "buy" else reference + distance
        return round(price, self.digits)

    def _relative_sl(self, stop_pips: float) -> Optional[int]:
        """Stop distance in the server's relative price units.

        A MARKET order has no price to hang an absolute stop on at submission
        time, and cTrader rejects `stopLoss` on one for exactly that reason -
        `relativeStopLoss` is the field for it, expressed as a distance on the
        same 1e5 scale prices use.
        """
        if stop_pips <= 0:
            return None
        if self.price_scale.source == "fallback":
            # Before detection the divisor is 1, which would turn 60 pips into
            # "1" and hand the broker a stop one point wide.
            raise ToolCallError(
                "Refusing to build a relative stop loss before the price scale "
                "has been detected - call bootstrap() first.")
        return int(round(stop_pips * self.pip_size * self.price_scale.divisor))

    async def market_order(self, side: str, lots: float, stop_pips: float) -> Any:
        # Relative, not absolute: see _relative_sl.
        return await self._submit("MARKET", side, lots, price=None,
                                  stop_loss=None,
                                  relative_stop_loss=self._relative_sl(stop_pips))

    async def stop_order(self, side: str, lots: float, price: float,
                         stop_pips: float) -> Any:
        # A pending STOP does have a known trigger price, so its stop is absolute.
        return await self._submit("STOP", side, lots, price=price,
                                  stop_loss=self._sl_for(side, price, stop_pips))

    async def _submit(self, order_type: str, side: str, lots: float,
                      *, price: Optional[float], stop_loss: Optional[float],
                      relative_stop_loss: Optional[int] = None) -> Any:
        tool = self._conn.catalog.require(*T_CREATE)
        lots = self.round_lots(lots)
        values: dict[str, Any] = {
            "symbol_id": self.symbol_id,
            "symbol_name": self._cfg.symbol,
            "order_type": ORDER_TYPE.get(order_type, Candidates.of(order_type)),
            "trade_side": SIDE[side],
            "volume": self.lots_to_wire(lots),
            "stop_loss": stop_loss,
            "label": self._cfg.label,
            "comment": self._cfg.label,
        }
        if price is not None:
            # A STOP order's trigger is `stopPrice`; `limitPrice` is a different
            # field and filling the wrong one silently changes the order type.
            values["stop_price"] = round(price, self.digits)
        if relative_stop_loss is not None:
            values["relative_stop_loss"] = relative_stop_loss

        args = bind_arguments(tool, values)
        if not self._cfg.is_live:
            log.info("[PAPER] would submit %s: %s", tool.name, args)
            return {"paper": True, "arguments": args}
        log.info("Submitting %s: %s", tool.name, args)
        return extract_payload(await self._conn.call_tool(tool.name, args))

    async def amend_stop_order(self, order_id: str, lots: float, price: float,
                               side: str, stop_pips: float) -> Any:
        lots = self.round_lots(lots)
        values = {
            "order_id": order_id,
            "volume": self.lots_to_wire(lots),
            "stop_price": round(price, self.digits),
            "stop_loss": self._sl_for(side, price, stop_pips),
        }
        if not self._cfg.is_live:
            log.info("[PAPER] would amend order %s -> %.2f", order_id, price)
            return {"paper": True}
        return await self._call(T_AMEND, values)

    async def cancel_order(self, order_id: str) -> Any:
        if not self._cfg.is_live:
            log.info("[PAPER] would cancel order %s", order_id)
            return {"paper": True}
        return await self._call(T_CANCEL, {"order_id": order_id})

    async def close_position(self, position_id: str, lots: float) -> Any:
        if not self._cfg.is_live:
            log.info("[PAPER] would close position %s (%.2f lots)", position_id, lots)
            return {"paper": True}
        return await self._call(T_CLOSE, {
            "position_id": position_id,
            "volume": self.lots_to_wire(lots),
        })
