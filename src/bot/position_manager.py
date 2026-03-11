import json
import math
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional


def _floor_to_step(qty: float, step_size: float) -> float:
    """Truncate qty down to the nearest valid lot-size step (never round up)."""
    if step_size <= 0:
        return round(qty, 6)
    precision = max(0, round(-math.log10(step_size)))
    factor    = 10 ** precision
    return math.floor(qty * factor) / factor

_DATA_DIR      = "/data" if os.path.isdir("/data") else os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
POSITIONS_FILE = os.path.join(_DATA_DIR, "positions.json")
STARTING_CAPITAL = 100.0
MIN_NOTIONAL     = 15.0


@dataclass
class Position:
    symbol:           str
    entry_price:      float
    quantity:         float
    usdt_size:        float
    stop_loss:        float
    tp1_price:        float
    tp1_hit:          bool  = False
    strategy:         str   = "momentum"
    entry_time:       str   = field(default_factory=lambda: datetime.utcnow().isoformat())
    candles_open:     int   = 0
    order_id:         str   = ""
    tp1_order_id:     str   = ""
    trail_high:       float = 0.0   # highest close seen since entry (trailing stop)
    breakeven_active: bool  = False  # True once stop pinned at entry price
    initial_risk:     float = 0.0   # entry_price - stop_loss at open (for R-multiple TP)

    def to_dict(self) -> dict:
        return asdict(self)


class PositionManager:
    def __init__(self):
        self.open:   dict[str, Position] = {}
        self.closed: list[dict]          = []
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
            "closed": self.closed[-200:],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    # ── Capital ────────────────────────────────────────────────────

    @property
    def capital_deployed(self) -> float:
        return sum(p.usdt_size for p in self.open.values())

    @property
    def capital_available(self) -> float:
        return max(0.0, self.starting_capital - self.capital_deployed)

    # ── Sizing ─────────────────────────────────────────────────────

    def calculate_size(self, entry_price: float, atr: float,
                       rules: dict = None,
                       stop_override: float = None,
                       step_size: float = 0.00001) -> tuple[float, float, float]:
        rules    = rules or {}
        risk_pct = rules.get("risk_per_trade_pct", 1.0) / 100.0

        if stop_override is not None:
            stop_loss = stop_override
        else:
            # ATR stop (always active)
            atr_stop_mult = rules.get("atr_stop_mult", 1.5)
            atr_stop      = entry_price - (atr_stop_mult * atr)

            # Fixed % stop — use tightest
            if rules.get("fixed_stop_enabled", False):
                fixed_pct  = rules.get("fixed_stop_pct", 3.0) / 100.0
                fixed_stop = entry_price * (1 - fixed_pct)
                stop_loss  = max(atr_stop, fixed_stop)
            else:
                stop_loss = atr_stop

        stop_dist = entry_price - stop_loss
        if stop_dist <= 0:
            return 0.0, 0.0, stop_loss

        risk_usdt = self.starting_capital * risk_pct
        usdt_size = risk_usdt / (stop_dist / entry_price)
        usdt_size = min(usdt_size, self.capital_available)

        if usdt_size < MIN_NOTIONAL:
            return 0.0, 0.0, stop_loss

        qty = _floor_to_step(usdt_size / entry_price, step_size)
        return qty, round(usdt_size, 2), round(stop_loss, 4)

    # ── Position lifecycle ─────────────────────────────────────────

    def open_position(self, symbol: str, entry_price: float, quantity: float,
                      usdt_size: float, stop_loss: float, tp1_price: float,
                      order_id: str = "", rules: dict = None,
                      strategy: str = "momentum") -> Position:
        initial_risk = max(entry_price - stop_loss, 0.0)
        pos = Position(
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            usdt_size=usdt_size,
            stop_loss=stop_loss,
            tp1_price=tp1_price,
            order_id=order_id,
            trail_high=entry_price,
            initial_risk=initial_risk,
            strategy=strategy,
        )
        self.open[symbol] = pos
        self._save()
        return pos

    def hit_tp1(self, symbol: str, exit_price: float,
                tp1_order_id: str = "", rules: dict = None):
        pos = self.open.get(symbol)
        if not pos:
            return
        rules        = rules or {}
        tp1_exit_pct = rules.get("tp1_exit_pct", 40.0) / 100.0
        qty_sold     = round(pos.quantity * tp1_exit_pct, 6)
        pnl_usdt     = (exit_price - pos.entry_price) * qty_sold

        pos.tp1_hit      = True
        pos.quantity     = round(pos.quantity - qty_sold, 6)
        pos.usdt_size    = round(pos.usdt_size * (1 - tp1_exit_pct), 2)
        pos.tp1_order_id = tp1_order_id

        # Breakeven stop: pin stop at entry after TP1
        if rules.get("breakeven_stop_enabled", False) and not pos.breakeven_active:
            pos.stop_loss       = pos.entry_price
            pos.breakeven_active = True

        self._record_partial(symbol, qty_sold, exit_price, pnl_usdt, "TP1")
        self._save()

    def close_position(self, symbol: str, exit_price: float, reason: str) -> Optional[dict]:
        pos = self.open.pop(symbol, None)
        if not pos:
            return None
        pnl_usdt = (exit_price - pos.entry_price) * pos.quantity
        pnl_pct  = (exit_price - pos.entry_price) / pos.entry_price * 100
        record = {
            "symbol":       symbol,
            "entry_price":  pos.entry_price,
            "exit_price":   exit_price,
            "quantity":     pos.quantity,
            "usdt_size":    pos.usdt_size,
            "pnl_usdt":     round(pnl_usdt, 4),
            "pnl_pct":      round(pnl_pct, 4),
            "reason":       reason,
            "entry_time":   pos.entry_time,
            "exit_time":    datetime.utcnow().isoformat(),
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
            "symbol":    symbol,
            "exit_price": price,
            "quantity":  qty,
            "pnl_usdt":  round(pnl, 4),
            "reason":    reason,
            "exit_time": datetime.utcnow().isoformat(),
            "partial":   True,
        })

    # ── Stats ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        closed = [t for t in self.closed if not t.get("partial")]
        wins   = [t for t in closed if t["pnl_usdt"] > 0]
        win_rate = len(wins) / len(closed) * 100 if closed else 0.0
        avg_pnl  = sum(t["pnl_usdt"] for t in closed) / len(closed) if closed else 0.0
        total_pnl   = sum(t["pnl_usdt"] for t in self.closed)
        current_val = self.starting_capital + total_pnl
        return {
            "starting_capital":  self.starting_capital,
            "current_value":     round(current_val, 2),
            "total_pnl":         round(total_pnl, 4),
            "total_pnl_pct":     round((total_pnl / self.starting_capital) * 100, 2),
            "trades_total":      len(closed),
            "trades_today":      self._trades_today(),
            "win_rate":          round(win_rate, 1),
            "avg_pnl_usdt":      round(avg_pnl, 4),
            "open_positions":    len(self.open),
            "capital_deployed":  round(self.capital_deployed, 2),
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
