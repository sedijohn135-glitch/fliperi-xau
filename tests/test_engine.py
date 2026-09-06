"""Tests for the pieces that decide how much money is at stake."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CTRADER_MCP_TOKEN", "test-token-value")
os.environ.setdefault("PANEL_PASSWORD", "test-password")

import pytest

from app.broker import Broker
from app.strategy import Engine
from config import load_config
from mcp_client.schema import ToolCatalog, ToolSpec, bind_arguments, Candidates
from models import PendingOrder, Position

# The real create_order schema, as the live server reports it.
CREATE_ORDER = ToolSpec("create_order", "", {"type": "object", "properties": {
    "symbolId": {"type": "integer"},
    "orderType": {"type": "string",
                  "enum": ["MARKET", "LIMIT", "STOP", "MARKET_RANGE", "STOP_LIMIT"]},
    "tradeSide": {"type": "string", "enum": ["BUY", "SELL"]},
    "volume": {"type": "integer"},
    "limitPrice": {"type": "number"}, "stopPrice": {"type": "number"},
    "stopLoss": {"type": "number"}, "takeProfit": {"type": "number"},
    "relativeStopLoss": {"type": "integer"}, "relativeTakeProfit": {"type": "integer"},
    "comment": {"type": "string"}, "label": {"type": "string"},
}, "required": ["symbolId", "orderType", "tradeSide", "volume"]})


class FakeConn:
    def __init__(self):
        self.catalog = ToolCatalog([CREATE_ORDER])
        self.is_ready = True


def broker():
    """A broker in the state bootstrap() leaves it in."""
    from utils.prices import Scale

    b = Broker(load_config(), FakeConn())
    b.bid, b.ask, b.balance = 4341.74, 4341.97, 1000.0
    b.price_scale = Scale(100_000.0, "detected")
    b.money_scale = Scale(100.0, "detected")
    return b


# --- the two bugs the recorded session exposed ---------------------------

def test_a_pip_on_gold_is_one_cent_not_ten():
    """The recorded session calls 4341.87 -> 4342.15 "~28 pips"."""
    cfg = load_config()
    assert cfg.pip_size == 0.01
    assert cfg.reverse_pips * cfg.pip_size == pytest.approx(0.30)
    assert cfg.stop_pips * cfg.pip_size == pytest.approx(0.60)
    assert cfg.trail_step_pips * cfg.pip_size == pytest.approx(0.05)


def test_market_orders_carry_a_relative_stop_not_an_absolute_one():
    """cTrader rejects an absolute stopLoss on a MARKET order."""
    b = broker()
    assert b._relative_sl(60) == 60 * 0.01 * 100_000       # 60000
    args = bind_arguments(CREATE_ORDER, {
        "symbol_id": 41, "order_type": Candidates.of("MARKET"),
        "trade_side": Candidates.of("BUY"), "volume": 100,
        "relative_stop_loss": b._relative_sl(60)})
    assert args["relativeStopLoss"] == 60_000
    assert "stopLoss" not in args


def test_a_relative_stop_is_refused_before_the_scale_is_known():
    """Divisor 1 would turn 60 pips into a one-point stop."""
    from mcp_client.errors import ToolCallError

    b = Broker(load_config(), FakeConn())
    with pytest.raises(ToolCallError, match="price scale"):
        b._relative_sl(60)


def test_pending_stops_carry_an_absolute_stop_loss():
    b = broker()
    sl = b._sl_for("sell", 4342.30, 60)
    assert sl == pytest.approx(4342.90)                     # 60 pips above
    assert b._sl_for("buy", 4342.30, 60) == pytest.approx(4341.70)


# --- volume --------------------------------------------------------------

def test_volume_goes_on_the_wire_as_centi_units():
    b = broker()
    assert b.lots_to_wire(0.01) == pytest.approx(100)        # 0.01 x 100oz x 100
    assert b.lots_to_wire(0.64) == pytest.approx(6400)
    assert b.wire_to_lots(b.lots_to_wire(0.08)) == pytest.approx(0.08)


# --- the martingale ladder ----------------------------------------------

def engine_with(positions, pending=(), **env):
    for k, v in env.items():
        os.environ[k] = str(v)
    cfg = load_config()
    b = broker()
    b.positions, b.pending = list(positions), list(pending)
    return Engine(cfg, b)


def pos(side, lots, entry, profit=None, label="XAU_SAR_MG"):
    return Position(position_id=str(abs(hash((side, lots, entry))) % 10**6),
                    symbol_id=41, symbol_name="XAUUSD", side=side, volume=lots,
                    entry_price=entry, label=label, profit=profit)


def test_a_losing_basket_multiplies_the_next_lot():
    e = engine_with([pos("BUY", 0.01, 4342.00, profit=-3.0)])
    assert e.basket_pnl() == pytest.approx(-3.0)
    assert e.step == 0
    assert e.next_lots() == pytest.approx(0.02)             # 0.01 x 2^1


def test_a_winning_basket_returns_to_the_base_lot():
    """Exactly what the recorded session shows: BUY 0.04 in profit, SELL STOP 0.01."""
    e = engine_with([pos("BUY", 0.04, 4339.68, profit=3.16)])
    assert e.basket_pnl() > 0
    assert e.next_lots() == pytest.approx(0.01)


def test_the_ladder_deepens_with_each_open_position():
    e = engine_with([pos("BUY", 0.01, 4342.0, profit=-2.0),
                     pos("SELL", 0.02, 4341.0, profit=-1.0)])
    assert e.step == 1
    assert e.next_lots() == pytest.approx(0.04)             # 0.01 x 2^2


def test_basket_pnl_falls_back_to_prices_when_the_broker_reports_none():
    e = engine_with([pos("SELL", 0.02, 4342.53, profit=None)])
    # 0.02 lots x 100 oz x (4342.53 - 4341.97 ask) = +1.12
    assert e.basket_pnl() == pytest.approx(1.12, abs=0.01)


# --- the limits ----------------------------------------------------------

def test_a_wide_spread_shuts_the_gate():
    e = engine_with([])
    e.api.bid, e.api.ask = 4341.00, 4341.50      # 50 pips
    assert e.spread_pips() == pytest.approx(50.0)
    assert not e.gates_open()
    e.api.ask = 4341.20                          # 20 pips
    assert e.gates_open()


def test_net_lots_nets_the_hedge_out():
    e = engine_with([pos("BUY", 0.04, 4340.0), pos("SELL", 0.02, 4341.0)])
    assert e.open_lots() == pytest.approx(0.06)
    assert e.net_lots() == pytest.approx(0.02)


def test_only_stop_orders_count_as_the_reverse_pending():
    pending = [
        PendingOrder("1", 41, "XAUUSD", "SELL", 0.01, 4341.0,
                     order_type="STOP", label="XAU_SAR_MG"),
        PendingOrder("2", 41, "XAUUSD", "BUY", 0.01, 4350.0,
                     order_type="LIMIT", label="XAU_SAR_MG"),
    ]
    e = engine_with([pos("BUY", 0.01, 4340.0)], pending)
    assert [o.order_id for o in e.orders] == ["1"]


def test_the_engine_never_starts_itself():
    assert engine_with([]).running is False
