"""
Bot engine — orchestrates the strategy loop.
All strategy parameters are live-read from rules.json each tick,
so changes in the dashboard take effect immediately.
"""

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from src.bot.indicators import klines_to_df, compute_indicators, get_signal, get_daytrading_signal
from src.bot.position_manager import PositionManager, MIN_NOTIONAL
from src.bot.rules import load_rules

KLINE_LIMIT        = 250
MAX_DAILY_LOSS_PCT = 3.0
TIME_STOP_CANDLES  = 20

# Sleep between loop ticks — shorter intervals need faster polling
INTERVAL_SLEEP = {
    "1m": 10, "3m": 15, "5m": 20, "15m": 30,
    "30m": 45, "1h": 60, "4h": 120, "1d": 300,
}


class BotEngine:
    def __init__(self, binance_client, username: str = "default"):
        self.client   = binance_client
        self.username = username
        self.pm       = PositionManager()
        self.running  = False
        self.thread   = None

        self._log_lock  = threading.Lock()
        self._log_deque: deque = deque(maxlen=500)
        self._stop_event = threading.Event()

        self._last_candle_time:    dict[str, int] = {}
        self._cooldown_until:      dict[str, int] = {}
        self._step_size_cache:     dict[str, float] = {}   # symbol -> lot step size
        self._global_candle_count: int = 0
        self._daily_loss_halt      = False
        self._daily_loss_reset_date: str = ""

        self.stats = {
            "trades_today": 0,
            "pnl_today":    0.0,
            "started_at":   None,
            "last_tick":    None,
            "next_tick_at": None,
        }

    # ── Public controls ────────────────────────────────────────────

    def start(self):
        if self.client is None:
            self._log("ERROR", "Cannot start: add Binance API keys in Settings")
            return False, "No Binance API keys configured — add them in Settings"
        if self.running:
            return False, "Already running"
        self._stop_event.clear()
        self.running = True
        self.stats["started_at"] = datetime.utcnow().isoformat()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        rules    = load_rules(self.username)
        strategy = rules.get("strategy", "momentum").upper()
        interval = rules.get("interval", "1h")
        self._log("INFO", f"Bot started — {strategy} strategy [{interval} candles]")
        return True, ""

    def stop(self):
        if not self.running:
            return False
        self.running = False
        self._stop_event.set()          # wake the sleeping thread immediately
        self.stats["started_at"]   = None
        self.stats["next_tick_at"] = None
        self._log("INFO", "Bot stopped by user")
        return True

    def get_stats(self) -> dict:
        return {**self.stats, **self.pm.get_stats(), "running": self.running, "halted": self._daily_loss_halt}

    def get_log(self, limit: int = 80) -> list:
        with self._log_lock:
            return list(self._log_deque)[:limit]

    # ── Main loop ──────────────────────────────────────────────────

    def _loop(self):
        while self.running:
            try:
                self._reset_daily_halt_if_new_day()
                if self._daily_loss_halt:
                    self._log("INFO", "Daily loss limit active — halted until UTC midnight")
                    self._stop_event.wait(timeout=60)
                    continue

                self.stats["last_tick"] = datetime.utcnow().isoformat()
                rules    = load_rules(self.username)
                interval = rules.get("interval", "1h")
                pairs    = rules.get("trade_pairs", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])

                for symbol in pairs:
                    if not self.running:
                        break
                    try:
                        self._process_pair(symbol, rules, interval)
                    except Exception as e:
                        self._log("ERROR", f"{symbol}: {e}")

                if not self.running:
                    break

                self.pm.increment_candles()
                self._global_candle_count += 1

                pm = self.pm.get_stats()
                self.stats["trades_today"] = pm["trades_today"]
                self.stats["pnl_today"]    = pm["total_pnl"]
                self._check_daily_loss(rules)

            except Exception as e:
                self._log("ERROR", f"Loop error: {e}")

            if not self.running:
                break

            sleep_s = INTERVAL_SLEEP.get(rules.get("interval", "1h"), 60)
            self.stats["next_tick_at"] = (datetime.utcnow() + timedelta(seconds=sleep_s)).isoformat()
            self._stop_event.wait(timeout=sleep_s)   # interruptible — wakes instantly on stop()
            self._stop_event.clear()

        self.stats["next_tick_at"] = None

    # ── Per-pair routing ───────────────────────────────────────────

    def _process_pair(self, symbol: str, rules: dict, interval: str = "1h"):
        if rules.get("strategy", "momentum") == "daytrading":
            self._process_pair_daytrading(symbol, rules, interval)
        else:
            self._process_pair_momentum(symbol, rules, interval)

    # ── Momentum strategy ──────────────────────────────────────────

    def _process_pair_momentum(self, symbol: str, rules: dict, interval: str = "1h"):
        raw = self.client.client.get_klines(symbol=symbol, interval=interval, limit=KLINE_LIMIT)
        df  = compute_indicators(klines_to_df(raw))
        if len(df) < 3:
            return

        if symbol in self.pm.open:
            self._monitor_position(symbol, df, rules)
            return

        # 24h volume filter
        if rules.get("min_volume_usdt_enabled", False):
            ticker  = self.client.client.get_ticker(symbol=symbol)
            vol_24h = float(ticker.get("quoteVolume", 0))
            min_vol = rules.get("min_volume_usdt", 5_000_000.0)
            if vol_24h < min_vol:
                self._log("INFO", f"{symbol} — skip: 24h vol ${vol_24h:,.0f} < ${min_vol:,.0f}")
                return

        # Spread filter
        if rules.get("max_spread_enabled", False):
            book       = self.client.client.get_order_book(symbol=symbol, limit=1)
            bid        = float(book["bids"][0][0])
            ask        = float(book["asks"][0][0])
            spread_pct = (ask - bid) / ((bid + ask) / 2) * 100
            if spread_pct > rules.get("max_spread_pct", 0.1):
                self._log("INFO", f"{symbol} — skip: spread {spread_pct:.3f}%")
                return

        # Cooldown filter
        if rules.get("cooldown_enabled", False):
            expires = self._cooldown_until.get(symbol, 0)
            if self._global_candle_count < expires:
                remaining = expires - self._global_candle_count
                self._log("INFO", f"{symbol} — cooldown: {remaining} candles remaining")
                return

        last_closed = int(raw[-2][0])
        if self._last_candle_time.get(symbol) == last_closed:
            return
        self._last_candle_time[symbol] = last_closed

        result = get_signal(df, rules)
        if not result["signal"]:
            failed = [k for k, v in result.get("checks", {}).items() if not v]
            if failed:
                self._log("INFO", f"{symbol} — no signal: {', '.join(failed)}")
            return

        if len(self.pm.open) >= rules.get("max_open_positions", 3):
            self._log("INFO", f"{symbol} — signal fired but max positions reached")
            return

        self._enter(symbol, result, rules)

    # ── Day trading strategy ───────────────────────────────────────

    def _process_pair_daytrading(self, symbol: str, rules: dict, interval: str = "1h"):
        raw = self.client.client.get_klines(symbol=symbol, interval=interval, limit=KLINE_LIMIT)
        df  = compute_indicators(klines_to_df(raw))
        if len(df) < 3:
            return

        if symbol in self.pm.open:
            pos = self.pm.open[symbol]
            if pos.strategy == "daytrading":
                self._monitor_position_daytrading(symbol, df, rules)
            else:
                self._monitor_position(symbol, df, rules)
            return

        # Spread filter (shared)
        if rules.get("max_spread_enabled", False):
            book       = self.client.client.get_order_book(symbol=symbol, limit=1)
            bid        = float(book["bids"][0][0])
            ask        = float(book["asks"][0][0])
            spread_pct = (ask - bid) / ((bid + ask) / 2) * 100
            if spread_pct > rules.get("max_spread_pct", 0.1):
                self._log("INFO", f"{symbol} — skip: spread {spread_pct:.3f}%")
                return

        # Cooldown (shared)
        if rules.get("cooldown_enabled", False):
            expires = self._cooldown_until.get(symbol, 0)
            if self._global_candle_count < expires:
                remaining = expires - self._global_candle_count
                self._log("INFO", f"{symbol} — cooldown: {remaining} candles remaining")
                return

        last_closed = int(raw[-2][0])
        if self._last_candle_time.get(symbol) == last_closed:
            return
        self._last_candle_time[symbol] = last_closed

        result = get_daytrading_signal(df, rules)
        if not result["signal"]:
            failed = [k for k, v in result.get("checks", {}).items() if not v]
            if failed:
                self._log("INFO", f"{symbol} — no signal: {', '.join(failed)}")
            return

        if len(self.pm.open) >= rules.get("max_open_positions", 3):
            self._log("INFO", f"{symbol} — signal fired but max positions reached")
            return

        self._enter_daytrading(symbol, result, rules)

    # ── Exchange info helpers ──────────────────────────────────────

    def _get_step_size(self, symbol: str) -> float:
        """Return lot-size step for symbol, cached after first fetch."""
        if symbol not in self._step_size_cache:
            try:
                info    = self.client.client.get_symbol_info(symbol)
                filters = {f['filterType']: f for f in info['filters']}
                step    = float(filters['LOT_SIZE']['stepSize'])
            except Exception:
                step = 0.00001
            self._step_size_cache[symbol] = step
        return self._step_size_cache[symbol]

    # ── Entry ──────────────────────────────────────────────────────

    def _enter(self, symbol: str, signal: dict, rules: dict):
        entry_price = signal["close"]
        atr         = signal["atr"]

        step = self._get_step_size(symbol)
        qty, usdt_size, stop_loss = self.pm.calculate_size(entry_price, atr, rules, step_size=step)
        if qty <= 0 or usdt_size < MIN_NOTIONAL:
            self._log("INFO", f"{symbol} — signal fired but position too small (${usdt_size:.2f})")
            return

        # Determine TP1 price — R-multiple > fixed % > ATR (in precedence order)
        initial_risk = entry_price - stop_loss
        if rules.get("r_multiple_tp_enabled", False):
            tp1_price = round(entry_price + rules.get("r_multiple", 2.0) * initial_risk, 4)
        elif rules.get("fixed_tp_enabled", False):
            tp1_price = round(entry_price * (1 + rules.get("fixed_tp_pct", 5.0) / 100.0), 4)
        else:
            tp1_price = round(entry_price + rules.get("atr_tp1_mult", 1.5) * atr, 4)

        try:
            order      = self.client.client.order_market_buy(symbol=symbol, quantity=qty)
            fills      = order.get("fills", [])
            fill_price = float(fills[0]["price"]) if fills else entry_price
        except Exception as e:
            self._log("ERROR", f"{symbol} — buy order failed: {e}")
            return

        pos = self.pm.open_position(
            symbol=symbol,
            entry_price=fill_price,
            quantity=qty,
            usdt_size=usdt_size,
            stop_loss=stop_loss,
            tp1_price=tp1_price,
            rules=rules,
        )

        self._log("TRADE",
            f"BUY {symbol} | qty={qty} | entry=${pos.entry_price:.4f} "
            f"| stop=${pos.stop_loss:.4f} | TP1=${pos.tp1_price:.4f} "
            f"| size=${usdt_size:.2f} | RSI={signal['rsi']:.1f} ADX={signal['adx']:.1f}"
        )

    # ── Day-trading entry ──────────────────────────────────────────

    def _enter_daytrading(self, symbol: str, signal: dict, rules: dict):
        entry_price = signal["close"]
        trail_pct   = rules.get("dt_trailing_stop_pct", 1.0)
        tp_pct      = rules.get("dt_take_profit_pct",   3.0)
        stop_loss   = round(entry_price * (1 - trail_pct / 100), 4)
        tp_price    = round(entry_price * (1 + tp_pct   / 100), 4)

        step = self._get_step_size(symbol)
        qty, usdt_size, _ = self.pm.calculate_size(
            entry_price, signal["atr"], rules, stop_override=stop_loss, step_size=step
        )
        if qty <= 0 or usdt_size < MIN_NOTIONAL:
            self._log("INFO", f"{symbol} — signal fired but position too small (${usdt_size:.2f})")
            return

        try:
            order      = self.client.client.order_market_buy(symbol=symbol, quantity=qty)
            fills      = order.get("fills", [])
            fill_price = float(fills[0]["price"]) if fills else entry_price
        except Exception as e:
            self._log("ERROR", f"{symbol} — buy order failed: {e}")
            return

        pos = self.pm.open_position(
            symbol=symbol, entry_price=fill_price, quantity=qty,
            usdt_size=usdt_size, stop_loss=stop_loss, tp1_price=tp_price,
            rules=rules, strategy="daytrading",
        )
        self._log("TRADE",
            f"BUY {symbol} [DAY TRADE] | qty={qty} | entry=${fill_price:.4f} "
            f"| stop=${stop_loss:.4f} ({trail_pct}% trail) | TP=${tp_price:.4f} ({tp_pct:.1f}%) "
            f"| size=${usdt_size:.2f} | rise={signal['price_rise_pct']:+.2f}%"
        )

    # ── Position monitoring ────────────────────────────────────────

    def _monitor_position(self, symbol: str, df, rules: dict):
        pos   = self.pm.open[symbol]
        c     = df.iloc[-2]
        close = float(c["close"])
        ema20 = float(c["ema20"])

        # Update trailing high-water mark and compute trail stop
        if rules.get("trailing_stop_enabled", False):
            if close > pos.trail_high:
                pos.trail_high = close
            trail_pct  = rules.get("trailing_stop_pct", 2.0) / 100.0
            trail_stop = pos.trail_high * (1 - trail_pct)
        else:
            trail_stop = 0.0

        # Effective stop = tightest of all active stops
        effective_stop = pos.stop_loss
        if rules.get("trailing_stop_enabled", False):
            effective_stop = max(effective_stop, trail_stop)
        if pos.breakeven_active:
            effective_stop = max(effective_stop, pos.entry_price)

        # ── Stop loss ──────────────────────────────────────────────
        if close <= effective_stop:
            reason = "TRAIL_STOP" if (
                rules.get("trailing_stop_enabled", False) and trail_stop >= pos.stop_loss
            ) else "STOP_LOSS"
            self._exit(symbol, close, reason, rules)
            return

        # ── TP1 ────────────────────────────────────────────────────
        if not pos.tp1_hit and close >= pos.tp1_price:
            tp1_qty = round(pos.quantity * (rules.get("tp1_exit_pct", 40.0) / 100.0), 6)
            try:
                order    = self.client.client.order_market_sell(symbol=symbol, quantity=tp1_qty)
                fills    = order.get("fills", [])
                tp1_fill = float(fills[0]["price"]) if fills else close
            except Exception as e:
                self._log("ERROR", f"{symbol} TP1 sell failed: {e}")
                tp1_fill = close

            self.pm.hit_tp1(symbol, tp1_fill, rules=rules)
            pnl = (tp1_fill - pos.entry_price) * tp1_qty
            self._log("TRADE",
                f"TP1 {symbol} | sold {rules.get('tp1_exit_pct',40):.0f}% "
                f"@ ${tp1_fill:.4f} | P&L ${pnl:+.4f}"
            )
            return

        # ── TP2: trail remaining on EMA20 ─────────────────────────
        if pos.tp1_hit and close < ema20:
            self._exit(symbol, close, "TP2_TRAIL", rules)
            return

        # ── Time stop ─────────────────────────────────────────────
        time_stop = rules.get("time_stop_candles", TIME_STOP_CANDLES)
        if pos.candles_open >= time_stop and close < pos.entry_price:
            self._exit(symbol, close, "TIME_STOP", rules)
            return

        # ── Heartbeat every 4 candles ─────────────────────────────
        if pos.candles_open % 4 == 0:
            upnl = (close - pos.entry_price) / pos.entry_price * 100
            self._log("INFO",
                f"{symbol} open {pos.candles_open}h | ${close:.4f} | "
                f"P&L {upnl:+.2f}% | stop=${effective_stop:.4f}"
            )

    def _monitor_position_daytrading(self, symbol: str, df, rules: dict):
        pos   = self.pm.open[symbol]
        close = float(df.iloc[-2]["close"])

        # Update trailing high
        if close > pos.trail_high:
            pos.trail_high = close

        trail_pct  = rules.get("dt_trailing_stop_pct", 0.75)
        trail_stop = pos.trail_high * (1 - trail_pct / 100)

        # Breakeven stop — once up dt_breakeven_pct%, slide stop to entry
        breakeven_pct = rules.get("dt_breakeven_pct", 0.5)
        if not pos.breakeven_active:
            if close >= pos.entry_price * (1 + breakeven_pct / 100):
                pos.stop_loss        = pos.entry_price
                pos.breakeven_active = True
                self._log("INFO", f"{symbol} [DT] breakeven stop activated at ${pos.entry_price:.4f}")

        effective_stop = max(pos.stop_loss, trail_stop)

        # Take profit — exit full position
        tp_pct   = rules.get("dt_take_profit_pct", 2.0)
        tp_price = pos.entry_price * (1 + tp_pct / 100)
        if close >= tp_price:
            self._exit(symbol, close, "TAKE_PROFIT", rules)
            return

        # Trailing / breakeven stop hit
        if close <= effective_stop:
            reason = "BREAKEVEN" if pos.breakeven_active and close <= pos.entry_price else "TRAIL_STOP"
            self._exit(symbol, close, reason, rules)
            return

        # Time stop — close losing trade after N candles
        time_stop = rules.get("dt_time_stop_candles", 20)
        if pos.candles_open >= time_stop and close < pos.entry_price:
            self._exit(symbol, close, "TIME_STOP", rules)
            return

        # Heartbeat every 4 candles
        if pos.candles_open % 4 == 0:
            upnl = (close - pos.entry_price) / pos.entry_price * 100
            self._log("INFO",
                f"{symbol} [DT] open {pos.candles_open} candles | ${close:.4f} "
                f"| P&L {upnl:+.2f}% | stop=${effective_stop:.4f} | TP=${tp_price:.4f}"
            )

    def _exit(self, symbol: str, price: float, reason: str, rules: dict = None):
        pos = self.pm.open.get(symbol)
        if not pos:
            return
        try:
            self.client.client.order_market_sell(symbol=symbol, quantity=pos.quantity)
        except Exception as e:
            self._log("ERROR", f"{symbol} exit sell failed: {e}")

        record = self.pm.close_position(symbol, price, reason)
        if record:
            icon = "✓" if record["pnl_usdt"] > 0 else "✗"
            self._log("TRADE",
                f"{icon} CLOSE {symbol} [{reason}] "
                f"entry=${record['entry_price']:.4f} exit=${record['exit_price']:.4f} "
                f"P&L ${record['pnl_usdt']:+.4f} ({record['pnl_pct']:+.2f}%)"
            )

        rules = rules or {}
        if rules.get("cooldown_enabled", False):
            candles = rules.get("cooldown_candles", 3)
            self._cooldown_until[symbol] = self._global_candle_count + candles

    # ── Kill-switch ────────────────────────────────────────────────

    def _check_daily_loss(self, rules: dict):
        pnl      = self.pm.get_stats()["total_pnl"]
        loss_pct = rules.get("daily_loss_limit_pct", MAX_DAILY_LOSS_PCT)
        limit    = self.pm.starting_capital * (loss_pct / 100)
        if pnl < -abs(limit):
            self._daily_loss_halt = True
            self._log("ERROR", f"Daily loss limit hit (${pnl:.2f}). Halted until UTC midnight.")

    def _reset_daily_halt_if_new_day(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._daily_loss_reset_date:
            self._daily_loss_reset_date = today
            if self._daily_loss_halt:
                self._daily_loss_halt = False
                self._log("INFO", "New UTC day — daily halt lifted, trading resumed")

    # ── Logging ───────────────────────────────────────────────────

    def _log(self, level: str, message: str):
        entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "level": level, "message": message}
        with self._log_lock:
            self._log_deque.appendleft(entry)
        print(f"[{entry['time']}] {level}: {message}")
