"""Cross-layer market data types.

These are deliberately plain, immutable and free of any transport concern: by the
time a value reaches one of these dataclasses it has already been unscaled into
real human units (4431.23, not 443123000).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class Candle:
    ts: datetime          # bar OPEN time, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)


@dataclass(frozen=True)
class Quote:
    symbol_id: int
    bid: float
    ask: float
    ts: Optional[datetime] = None

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2.0


@dataclass(frozen=True)
class SymbolInfo:
    symbol_id: int
    name: str
    description: str = ""
    digits: Optional[int] = None
    lot_size: Optional[float] = None
    min_volume: Optional[float] = None
    max_volume: Optional[float] = None
    volume_step: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountSnapshot:
    balance: float
    equity: Optional[float] = None
    currency: str = "USD"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def equity_or_balance(self) -> float:
        return self.equity if self.equity is not None else self.balance


@dataclass(frozen=True)
class Position:
    position_id: str
    symbol_id: Optional[int]
    symbol_name: str
    side: str                 # "BUY" | "SELL"
    volume: float
    entry_price: Optional[float]
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    label: str = ""
    open_time: Optional[datetime] = None
    profit: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    symbol_id: Optional[int]
    symbol_name: str
    side: str
    volume: float
    price: Optional[float]
    order_type: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    label: str = ""
    created_time: Optional[datetime] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Deal:
    deal_id: str
    symbol_id: Optional[int]
    position_id: Optional[str]
    volume: float
    price: Optional[float]
    profit: float               # net, already unscaled
    commission: float = 0.0
    swap: float = 0.0
    executed_at: Optional[datetime] = None
    label: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def net_profit(self) -> float:
        return self.profit
