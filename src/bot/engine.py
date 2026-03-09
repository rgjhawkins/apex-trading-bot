"""
Bot engine — orchestrates the strategy loop.

Flow (runs every 60 seconds):
  1. Check if a new 1H candle has closed since last run
  2. For each pair: fetch klines, compute indicators, evaluate signal
  3. Open new positions if signal fires and capital is available
  4. Monitor open positions for stop, TP1, TP2 (trailing EMA20), time-stop exits
  5. Enforce daily loss limit kill-switch
"""

import threading
import time
from datetime import datetime, timezone

from src.bot.indicators import klines_to_df, compute_indicators, get_signal
from src.bot.position_manager import PositionManager, MIN_NOTIONAL
from src.bot.rules import load_rules


INTERVAL      = "1h"
KLINE_LIMIT   = 250           # enough history for EMA(200)
LOOP_SLEEP    = 60            # seconds between ticks
MAX_DAILY_LOSS_PCT = 3.0      # kill-switch threshold (% of starting capital)
TIME_STOP_CANDLES  = 20       # exit losing position after N candles


class BotEngine:
    def __init__(self, binance_client):
        self.client   = binance_client
        self.pm       = PositionManager()
        self.running  = False
        self.thread   = None
        self.log      = []
        self._last_candle_time: dict[str, int] = {}   # symbol → last processed open_time
        self._daily_loss_halt  = False
        self._daily_loss_reset_date: str = ""

        self.stats = {
            "trades_today": 0,
            "pnl_today":    0.0,
            "started_at":   None,
            "last_tick":    None,
        }

    # ── Public controls ────────────────────────────────────────────

    def start(self):
        if self.client is None:
            self._log("ERROR", "Cannot start: Binance client unavailable")
            return False
        if self.running:
            return False
        self.running = True
        self.stats["started_at"] = datetime.utcnow().isoformat()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self._log("INFO", "Bot engine started — strategy: RSI Momentum + EMA Trend Filter")
        return True

    def stop(self):
        if not self.running:
            return False
        self.running = False
        self.stats["started_at"] = None
        self._log("INFO", "Bot engine stopped by user")
        return True

    def get_stats(self) -> dict:
        pm_stats = self.pm.get_stats()
        return {
            **self.stats,
            **pm_stats,
            "running":     self.running,
            "halted":      self._daily_loss_halt,
        }

    def get_log(self, limit: int = 80) -> list:
        return self.log[:limit]

    # ── Main loop ──────────────────────────────────────────────────

    def _loop(self):
        while self.running:
            try:
                self._reset_daily_halt_if_new_day()
                if self._daily_loss_halt:
                    self._log("INFO", "Daily loss limit active — trading halted until UTC midnight")
                    time.sleep(LOOP_SLEEP)
                    continue

                self.stats["last_tick"] = datetime.utcnow().isoformat()
                rules = load_rules()
                pairs = rules.get("trade_pairs", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])

                for symbol in pairs:
                    try:
                        self._process_pair(symbol, rules)
                    except Exception as e:
                        self._log("ERROR", f"{symbol}: {e}")

                # Update candle counter for all open positions
                self.pm.increment_candles()

                # Sync stats
                pm_stats = self.pm.get_stats()
                self.stats["trades_today"] = pm_stats["trades_today"]
                self.stats["pnl_today"]    = pm_stats["total_pnl"]

                # Daily loss kill-switch check
                self._check_daily_loss()

            except Exception as e:
                self._log("ERROR", f"Loop error: {e}")

            time.sleep(LOOP_SLEEP)

    # ── Per-pair processing ────────────────────────────────────────

    def _process_pair(self, symbol: str, rules: dict):
        raw    = self.client.client.get_klines(symbol=symbol, interval=INTERVAL, limit=KLINE_LIMIT)
        df     = klines_to_df(raw)
        df     = compute_indicators(df)

        if len(df) < 3:
            return

        # The last closed candle's open_time (in ms from raw data)
        last_closed_open_time = int(raw[-2][0])

        # ── Monitor existing position ──────────────────────────────
        if symbol in self.pm.open:
            self._monitor_position(symbol, df, rules)
            return   # don't open a new position while one is active on this pair

        # ── Evaluate new entry (only on a fresh candle) ────────────
        if self._last_candle_time.get(symbol) == last_closed_open_time:
            return   # already evaluated this candle for this pair

        self._last_candle_time[symbol] = last_closed_open_time

        result = get_signal(df, rules)

        if not result["signal"]:
            # Log why (only log one failed check to avoid spam)
            failed = [k for k, v in result.get("checks", {}).items() if not v]
            if failed:
                self._log("INFO", f"{symbol} — no signal: {failed[0]}")
            return

        max_open = rules.get("max_open_positions", 3)
        if len(self.pm.open) >= max_open:
            self._log("INFO", f"{symbol} — signal fired but max positions ({max_open}) reached")
            return

        # Place entry order
        self._enter(symbol, result, rules)

    # ── Entry ──────────────────────────────────────────────────────

    def _enter(self, symbol: str, signal: dict, rules: dict):
        entry_price = signal["close"]
        atr         = signal["atr"]

        qty, usdt_size, stop_loss = self.pm.calculate_size(entry_price, atr, rules)

        if qty <= 0 or usdt_size < MIN_NOTIONAL:
            self._log("INFO", f"{symbol} — signal fired but position size too small (${usdt_size:.2f})")
            return

        atr_tp1_mult = rules.get("atr_tp1_mult", 1.5)
        tp1_price = round(entry_price + (atr_tp1_mult * atr), 4)

        try:
            order = self.client.client.order_market_buy(
                symbol=symbol,
                quantity=qty,
            )
            order_id = str(order.get("orderId", ""))
            actual_price = float(order.get("fills", [{}])[0].get("price", entry_price)) if order.get("fills") else entry_price
        except Exception as e:
            self._log("ERROR", f"{symbol} — order failed: {e}")
            return

        pos = self.pm.open_position(
            symbol=symbol,
            entry_price=actual_price or entry_price,
            quantity=qty,
            usdt_size=usdt_size,
            atr=atr,
            order_id=order_id,
            rules=rules,
        )

        self._log("TRADE",
            f"BUY {symbol} | qty={qty} | entry=${pos.entry_price:.4f} "
            f"| stop=${pos.stop_loss:.4f} | TP1=${pos.tp1_price:.4f} "
            f"| size=${usdt_size:.2f} | RSI={signal['rsi']:.1f} ADX={signal['adx']:.1f}"
        )

    # ── Position monitoring ────────────────────────────────────────

    def _monitor_position(self, symbol: str, df, rules: dict):
        pos      = self.pm.open[symbol]
        c        = df.iloc[-2]   # last closed candle
        close    = c["close"]
        ema20    = c["ema20"]

        # ── Stop loss ──────────────────────────────────────────────
        if close <= pos.stop_loss:
            self._exit(symbol, close, "STOP_LOSS")
            return

        # ── TP1 (40% exit) ─────────────────────────────────────────
        if not pos.tp1_hit and close >= pos.tp1_price:
            tp1_qty = round(pos.quantity * 0.40, 6)
            try:
                order = self.client.client.order_market_sell(
                    symbol=symbol,
                    quantity=tp1_qty,
                )
                tp1_fill = float(order.get("fills", [{}])[0].get("price", close)) if order.get("fills") else close
            except Exception as e:
                self._log("ERROR", f"{symbol} TP1 sell failed: {e}")
                tp1_fill = close

            self.pm.hit_tp1(symbol, tp1_fill, rules=rules)
            self._log("TRADE",
                f"TP1 {symbol} | sold 40% @ ${tp1_fill:.4f} "
                f"| PnL ${(tp1_fill - pos.entry_price) * tp1_qty:.2f}"
            )
            return

        # ── TP2 trailing: exit remaining 60% when price closes below EMA20 ──
        if pos.tp1_hit and close < ema20:
            self._exit(symbol, close, "TP2_TRAIL")
            return

        # ── Time stop: exit losing position after N candles ───────
        time_stop = rules.get("time_stop_candles", TIME_STOP_CANDLES)
        if pos.candles_open >= time_stop and close < pos.entry_price:
            self._exit(symbol, close, "TIME_STOP")
            return

        # ── Log heartbeat every 4 candles ─────────────────────────
        if pos.candles_open % 4 == 0:
            unrealised_pct = (close - pos.entry_price) / pos.entry_price * 100
            self._log("INFO",
                f"{symbol} open {pos.candles_open}h | "
                f"price=${close:.4f} | P&L {unrealised_pct:+.2f}% | "
                f"{'TP1 hit ✓' if pos.tp1_hit else f'TP1@${pos.tp1_price:.4f}'}"
            )

    def _exit(self, symbol: str, price: float, reason: str):
        pos = self.pm.open.get(symbol)
        if not pos:
            return

        try:
            self.client.client.order_market_sell(
                symbol=symbol,
                quantity=pos.quantity,
            )
        except Exception as e:
            self._log("ERROR", f"{symbol} exit sell failed: {e}")

        record = self.pm.close_position(symbol, price, reason)
        if record:
            emoji  = "✓" if record["pnl_usdt"] > 0 else "✗"
            self._log("TRADE",
                f"{emoji} CLOSE {symbol} [{reason}] | "
                f"entry=${record['entry_price']:.4f} exit=${record['exit_price']:.4f} "
                f"| PnL ${record['pnl_usdt']:+.4f} ({record['pnl_pct']:+.2f}%)"
            )

    # ── Kill-switch ────────────────────────────────────────────────

    def _check_daily_loss(self):
        rules      = load_rules()
        pnl_today  = self.pm.get_stats()["total_pnl"]
        loss_pct   = rules.get("daily_loss_limit_pct", MAX_DAILY_LOSS_PCT)
        loss_limit = self.pm.starting_capital * (loss_pct / 100)
        if pnl_today < -abs(loss_limit):
            self._daily_loss_halt = True
            self._log("ERROR",
                f"Daily loss limit hit (${pnl_today:.2f}). "
                f"Trading halted until UTC midnight."
            )

    def _reset_daily_halt_if_new_day(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._daily_loss_reset_date:
            self._daily_loss_reset_date = today
            if self._daily_loss_halt:
                self._daily_loss_halt = False
                self._log("INFO", "New UTC day — daily loss halt lifted, trading resumed")

    # ── Logging ───────────────────────────────────────────────────

    def _log(self, level: str, message: str):
        entry = {
            "time":    datetime.utcnow().strftime("%H:%M:%S"),
            "level":   level,
            "message": message,
        }
        self.log.insert(0, entry)
        if len(self.log) > 500:
            self.log = self.log[:500]
        print(f"[{entry['time']}] {level}: {message}")
