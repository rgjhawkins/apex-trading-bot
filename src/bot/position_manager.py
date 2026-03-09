"""
Tracks all open and closed positions for the bot session.
Positions are held in memory; summary stats are written to positions.json
so the dashboard can read them without touching the exchange.
"""

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional

POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "../../positions.json")

STARTING_CAPITAL = 100.0      # USDT — the budget we trade with
MIN_NOTIONAL     = 15.0       # Binance minimum order size (USDT)


@dataclass
class Position:
    symbol:        str
    entry_price:   float
    quantity:      float          # base asset amount
    usdt_size:     float          # USDT value at entry
    stop_loss:     float
    tp1_price:     float
    tp1_hit:       bool  = False
    entry_time:    str   = field(default_factory=lambda: datetime.utcnow().isoformat())
    candles_open:  int   = 0
    order_id:      str   = ""
    tp1_order_id:  str   = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def unrealised_pnl_pct(self, current_price: float = 0.0) -> float:
        if current_price <= 0:
            return 0.0
        return (current_price - self.entry_price) / self.entry_price * 100


class PositionManager:
    def __init__(self):
        self.open:   dict[str, Position] = {}   # symbol → Position
        self.closed: list[dict]          = []   # completed trade records
        self.starting_capital = STARTING_CAPITAL
        self._load()

    # ── Persistence ────────────────────────────────────────────────

    def _load(self):
        path = os.path.abspath(POSITIONS_FILE)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                self.closed = data.get("closed", [])
                self.starting_capital = data.get("starting_capital", STARTING_CAPITAL)
            except Exception:
                pass

    def _save(self):
        path = os.path.abspath(POSITIONS_FILE)
        data = {
            "starting_capital": self.starting_capital,
            "open":   {s: p.to_dict() for s, p in self.open.items()},
            "closed": self.closed[-200:],   # keep last 200 trades
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    # ── Capital & sizing ───────────────────────────────────────────

    @property
    def capital_deployed(self) -> float:
        return sum(p.usdt_size for p in self.open.values())

    @property
    def capital_available(self) -> float:
        return max(0.0, self.starting_capital - self.capital_deployed)

    def calculate_size(self, entry_price: float, atr: float, rules: dict = None) -> tuple[float, float, float]:
        """
        Returns (quantity_base, usdt_size, stop_loss_price).
        Uses fixed-fractional: risk N% of starting_capital per trade.
        """
        if rules is None:
            rules = {}
        atr_stop_mult = rules.get("atr_stop_mult",      1.5)
        risk_pct      = rules.get("risk_per_trade_pct", 1.0) / 100.0

        stop_loss   = entry_price - (atr_stop_mult * atr)
        stop_dist   = entry_price - stop_loss
        risk_usdt   = self.starting_capital * risk_pct
        usdt_size   = risk_usdt / (stop_dist / entry_price)

        # Clamp to available capital
        usdt_size = min(usdt_size, self.capital_available)

        if usdt_size < MIN_NOTIONAL:
            return 0.0, 0.0, stop_loss     # too small, skip

        quantity = usdt_size / entry_price
        return round(quantity, 6), round(usdt_size, 2), round(stop_loss, 4)

    # ── Position lifecycle ─────────────────────────────────────────

    def open_position(self, symbol: str, entry_price: float, quantity: float,
                      usdt_size: float, atr: float, order_id: str = "",
                      rules: dict = None) -> Position:
        if rules is None:
            rules = {}
        atr_stop_mult = rules.get("atr_stop_mult", 1.5)
        atr_tp1_mult  = rules.get("atr_tp1_mult",  1.5)
        stop_loss  = round(entry_price - (atr_stop_mult * atr), 4)
        tp1_price  = round(entry_price + (atr_tp1_mult  * atr), 4)
        pos = Position(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            usdt_size=usdt_size,
            stop_loss=stop_loss,
            tp1_price=tp1_price,
            order_id=order_id,
        )
        self.open[symbol] = pos
        self._save()
        return pos

    def hit_tp1(self, symbol: str, exit_price: float, tp1_order_id: str = "",
                rules: dict = None):
        """Mark TP1 as hit, reduce position by tp1_exit_pct."""
        pos = self.open.get(symbol)
        if not pos:
            return
        tp1_exit_pct = (rules or {}).get("tp1_exit_pct", 40.0) / 100.0
        qty_sold   = round(pos.quantity * tp1_exit_pct, 6)
        pnl_usdt   = (exit_price - pos.entry_price) * qty_sold
        pos.tp1_hit      = True
        pos.quantity     = round(pos.quantity - qty_sold, 6)
        pos.usdt_size    = round(pos.usdt_size * (1 - tp1_exit_pct), 2)
        pos.tp1_order_id = tp1_order_id
        self._record_partial(symbol, qty_sold, exit_price, pnl_usdt, "TP1")
        self._save()

    def close_position(self, symbol: str, exit_price: float, reason: str):
        pos = self.open.pop(symbol, None)
        if not pos:
            return
        pnl_usdt = (exit_price - pos.entry_price) * pos.quantity
        pnl_pct  = (exit_price - pos.entry_price) / pos.entry_price * 100
        record = {
            "symbol":      symbol,
            "entry_price": pos.entry_price,
            "exit_price":  exit_price,
            "quantity":    pos.quantity,
            "usdt_size":   pos.usdt_size,
            "pnl_usdt":    round(pnl_usdt, 4),
            "pnl_pct":     round(pnl_pct, 4),
            "reason":      reason,
            "entry_time":  pos.entry_time,
            "exit_time":   datetime.utcnow().isoformat(),
            "candles_held": pos.candles_open,
        }
        self.closed.append(record)
        self._save()
        return record

    def increment_candles(self):
        for pos in self.open.values():
            pos.candles_open += 1

    def _record_partial(self, symbol, qty, price, pnl, reason):
        self.closed.append({
            "symbol":     symbol,
            "exit_price": price,
            "quantity":   qty,
            "pnl_usdt":   round(pnl, 4),
            "reason":     reason,
            "exit_time":  datetime.utcnow().isoformat(),
            "partial":    True,
        })

    # ── Stats ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        closed = [t for t in self.closed if not t.get("partial")]
        if not closed:
            win_rate = 0.0
            avg_pnl  = 0.0
        else:
            wins     = [t for t in closed if t["pnl_usdt"] > 0]
            win_rate = len(wins) / len(closed) * 100
            avg_pnl  = sum(t["pnl_usdt"] for t in closed) / len(closed)

        total_pnl   = sum(t["pnl_usdt"] for t in self.closed)
        current_val = self.starting_capital + total_pnl

        return {
            "starting_capital": self.starting_capital,
            "current_value":    round(current_val, 2),
            "total_pnl":        round(total_pnl, 4),
            "total_pnl_pct":    round((total_pnl / self.starting_capital) * 100, 2),
            "trades_total":     len(closed),
            "trades_today":     self._trades_today(),
            "win_rate":         round(win_rate, 1),
            "avg_pnl_usdt":     round(avg_pnl, 4),
            "open_positions":   len(self.open),
            "capital_deployed": round(self.capital_deployed, 2),
            "capital_available": round(self.capital_available, 2),
        }

    def get_open_positions(self) -> list:
        return [p.to_dict() for p in self.open.values()]

    def get_closed_trades(self, limit: int = 50) -> list:
        return list(reversed(self.closed[-limit:]))

    def _trades_today(self) -> int:
        today = datetime.utcnow().date().isoformat()
        return sum(
            1 for t in self.closed
            if not t.get("partial") and t.get("exit_time", "").startswith(today)
        )
