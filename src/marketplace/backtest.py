"""
Strategy backtester — simulates a trading strategy against historical
Binance kline data using the same indicators and signal logic as the
live engine.

Run against up to 1000 candles per pair/interval combination.
Starting capital: 10,000 USDT (paper).
"""

import math
from datetime import datetime

from src.bot.indicators import (
    klines_to_df, compute_indicators,
    get_signal, get_daytrading_signal,
)

STARTING_CAPITAL = 10_000.0   # USDT — paper trading baseline
WARMUP_CANDLES   = 200        # candles needed for EMA-200 to stabilise


def _floor_step(qty: float, step: float = 0.00001) -> float:
    if step <= 0:
        return round(qty, 6)
    precision = max(0, round(-math.log10(step)))
    factor    = 10 ** precision
    return math.floor(qty * factor) / factor


def run_backtest(klines_raw: list, rules: dict,
                 pair: str = "BTCUSDT") -> dict:
    """
    Simulate the strategy on historical kline data.

    Parameters
    ----------
    klines_raw : raw list from client.get_klines(...)
    rules      : strategy rules dict (same format as live rules)
    pair       : symbol name (for labelling trades)

    Returns a dict with statistics and the trade list.
    """
    df = klines_to_df(klines_raw)
    df = compute_indicators(df)

    if len(df) < WARMUP_CANDLES + 10:
        return {"error": f"Need at least {WARMUP_CANDLES + 10} candles, got {len(df)}"}

    strategy          = rules.get("strategy", "momentum")
    capital           = STARTING_CAPITAL
    peak_capital      = STARTING_CAPITAL
    max_drawdown_pct  = 0.0
    max_positions     = int(rules.get("max_open_positions", 3))

    open_positions: list[dict] = []
    closed_trades:  list[dict] = []
    equity_curve:   list[dict] = []

    for i in range(WARMUP_CANDLES, len(df) - 1):
        subset        = df.iloc[: i + 2]   # candles up to and including current
        current_bar   = df.iloc[i]
        current_close = float(current_bar["close"])
        candle_time   = str(df.index[i])

        # ── Manage open positions ──────────────────────────────────
        still_open: list[dict] = []
        for pos in open_positions:
            entry      = pos["entry_price"]
            atr        = float(current_bar["atr"])
            pos["candles_held"] += 1

            # Update high-water mark
            pos["highest_price"] = max(pos["highest_price"], current_close)
            high = pos["highest_price"]

            # Build stop price
            atr_stop = entry - atr * rules.get("atr_stop_mult", 2.0)
            if rules.get("breakeven_stop_enabled", True) and pos.get("tp1_hit"):
                atr_stop = max(atr_stop, entry)

            trailing_stop = None
            if rules.get("trailing_stop_enabled", True):
                trail_pct     = rules.get("trailing_stop_pct", 2.5) / 100.0
                trailing_stop = high * (1.0 - trail_pct)

            stop_price = atr_stop
            if trailing_stop is not None:
                stop_price = max(stop_price, trailing_stop)

            # TP levels
            tp1_price    = entry + atr * rules.get("atr_tp1_mult", 2.5)
            init_risk    = entry - pos["initial_stop"]
            r_mult_tp    = entry + init_risk * rules.get("r_multiple", 2.0) if init_risk > 0 else None

            exit_price  = None
            exit_reason = None

            # ── Check exits ────────────────────────────────────────
            if current_close <= stop_price:
                exit_price  = stop_price
                exit_reason = "stop_loss"

            elif not pos.get("tp1_hit") and current_close >= tp1_price:
                # Partial TP1 — exit a portion now
                tp1_exit_pct = rules.get("tp1_exit_pct", 50.0) / 100.0
                tp1_qty      = pos["qty"] * tp1_exit_pct
                tp1_cost     = tp1_qty * entry
                tp1_pnl      = (tp1_price - entry) * tp1_qty
                capital     += tp1_cost + tp1_pnl
                pos["tp1_pnl"]  = tp1_pnl
                pos["tp1_hit"]  = True
                pos["qty"]     *= (1.0 - tp1_exit_pct)
                pos["cost"]    *= (1.0 - tp1_exit_pct)
                if rules.get("breakeven_stop_enabled", True):
                    pos["initial_stop"] = entry

            elif r_mult_tp and current_close >= r_mult_tp:
                exit_price  = r_mult_tp
                exit_reason = "take_profit"

            # Time stop — only on losing positions
            if (exit_price is None
                    and pos["candles_held"] >= rules.get("time_stop_candles", 24)
                    and current_close < entry):
                exit_price  = current_close
                exit_reason = "time_stop"

            if exit_price is not None:
                pnl      = (exit_price - entry) / entry * pos["cost"]
                tp1_pnl  = pos.get("tp1_pnl", 0.0)
                capital += pos["cost"] + pnl
                total_pnl = pnl + tp1_pnl

                closed_trades.append({
                    "pair":          pair,
                    "entry_price":   round(entry, 6),
                    "exit_price":    round(exit_price, 6),
                    "entry_time":    pos["entry_time"],
                    "exit_time":     candle_time,
                    "pnl_pct":       round((exit_price - entry) / entry * 100, 2),
                    "pnl_usdt":      round(total_pnl, 2),
                    "reason":        exit_reason,
                    "candles_held":  pos["candles_held"],
                })

                peak_capital     = max(peak_capital, capital)
                drawdown         = (peak_capital - capital) / peak_capital * 100
                max_drawdown_pct = max(max_drawdown_pct, drawdown)
            else:
                still_open.append(pos)

        open_positions = still_open

        # Equity snapshot every 20 candles
        if i % 20 == 0:
            unrealised = sum(
                (current_close - p["entry_price"]) / p["entry_price"] * p["cost"]
                for p in open_positions
            )
            equity_curve.append({
                "time":   candle_time[:10],
                "equity": round(capital + sum(p["cost"] for p in open_positions) + unrealised, 2),
            })

        # ── Check for new entry ────────────────────────────────────
        if len(open_positions) >= max_positions:
            continue

        sig = (get_daytrading_signal(subset, rules)
               if strategy == "daytrading"
               else get_signal(subset, rules))

        if not sig.get("signal"):
            continue

        atr          = sig.get("atr", float(current_bar["atr"]))
        stop_price   = current_close - atr * rules.get("atr_stop_mult", 2.0)
        risk_pct     = rules.get("risk_per_trade_pct", 1.0) / 100.0
        risk_capital = capital * risk_pct
        risk_per_unit = current_close - stop_price

        if risk_per_unit <= 0:
            continue

        qty  = risk_capital / risk_per_unit
        cost = qty * current_close

        # Cap at 90% of available capital
        if cost > capital * 0.9:
            cost = capital * 0.9
            qty  = cost / current_close

        if cost < 10.0:   # minimum notional guard
            continue

        capital -= cost
        open_positions.append({
            "entry_price":   current_close,
            "entry_time":    candle_time,
            "qty":           qty,
            "cost":          cost,
            "initial_stop":  stop_price,
            "highest_price": current_close,
            "tp1_hit":       False,
            "tp1_pnl":       0.0,
            "candles_held":  0,
        })

    # ── Close any remaining positions at the last bar ──────────────
    if len(df) > 0:
        last_price = float(df.iloc[-1]["close"])
        last_time  = str(df.index[-1])
        for pos in open_positions:
            pnl      = (last_price - pos["entry_price"]) / pos["entry_price"] * pos["cost"]
            capital += pos["cost"] + pnl + pos.get("tp1_pnl", 0.0)
            closed_trades.append({
                "pair":         pair,
                "entry_price":  round(pos["entry_price"], 6),
                "exit_price":   round(last_price, 6),
                "entry_time":   pos["entry_time"],
                "exit_time":    last_time,
                "pnl_pct":      round((last_price - pos["entry_price"]) / pos["entry_price"] * 100, 2),
                "pnl_usdt":     round(pnl + pos.get("tp1_pnl", 0.0), 2),
                "reason":       "end_of_data",
                "candles_held": pos["candles_held"],
            })

    # ── Compute aggregate statistics ───────────────────────────────
    n       = len(closed_trades)
    winners = [t for t in closed_trades if t["pnl_usdt"] > 0]
    losers  = [t for t in closed_trades if t["pnl_usdt"] <= 0]

    win_rate      = len(winners) / n * 100 if n else 0.0
    total_pnl     = capital - STARTING_CAPITAL
    total_return  = total_pnl / STARTING_CAPITAL * 100

    avg_win  = sum(t["pnl_usdt"] for t in winners) / len(winners) if winners else 0.0
    avg_loss = sum(t["pnl_usdt"] for t in losers)  / len(losers)  if losers  else 0.0

    gross_win  = sum(t["pnl_usdt"] for t in winners)
    gross_loss = abs(sum(t["pnl_usdt"] for t in losers))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    # Add final equity point
    equity_curve.append({"time": "end", "equity": round(capital, 2)})

    return {
        "pair":             pair,
        "strategy":         strategy,
        "total_candles":    len(df) - WARMUP_CANDLES,
        "total_trades":     n,
        "win_rate":         round(win_rate, 1),
        "total_return_pct": round(total_return, 2),
        "total_pnl_usdt":   round(total_pnl, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "avg_win_usdt":     round(avg_win, 2),
        "avg_loss_usdt":    round(avg_loss, 2),
        "profit_factor":    profit_factor,
        "starting_capital": STARTING_CAPITAL,
        "final_capital":    round(capital, 2),
        "trades":           closed_trades,          # full trade list
        "equity_curve":     equity_curve,
    }
