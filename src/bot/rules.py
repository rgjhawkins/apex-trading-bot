import json
import os

# Use /data (Railway persistent volume) if it exists, otherwise fall back to
# the project root for local development.
_RULES_DIR = "/data" if os.path.isdir("/data") else os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))

DEFAULT_RULES = {
    # ── Strategy ───────────────────────────────────────────────────
    "strategy":                 "daytrading", # "momentum" | "daytrading"

    # ── Pairs & execution ──────────────────────────────────────────
    "trade_pairs":              ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"],
    "interval":                 "1h",      # candle timeframe: 1m 5m 15m 30m 1h 4h 1d
    "max_open_positions":       3,

    # ── Position sizing ────────────────────────────────────────────
    "risk_per_trade_pct":       1.0,       # % of capital risked per trade

    # ── Entry filters ──────────────────────────────────────────────
    "rsi_enabled":              True,      # use RSI dip + cross signal
    "rsi_dip_low":              38.0,      # deeper dip = better pullback entry
    "rsi_dip_high":             52.0,
    "rsi_cross_level":          48.0,      # cross triggers slightly below 50
    "adx_enabled":              True,      # require minimum ADX trend strength
    "adx_min":                  25.0,      # 25+ = confirmed trend, avoids choppy markets

    "volume_spike_enabled":     True,      # require volume spike on entry candle
    "volume_spike_mult":        1.5,       # entry volume must be > 1.5× 20-period average

    "macd_filter_enabled":      True,      # require MACD confirmation
    "macd_mode":                "positive",# histogram > 0 = momentum aligned

    "body_filter_enabled":      True,      # require strong candle body (no dojis)
    "body_filter_pct":          50.0,      # candle body must be > 50% of high-low range

    "min_volume_usdt_enabled":  False,     # skip low-volume pairs (off: testnet data limited)
    "min_volume_usdt":          10000000.0,# minimum $10M daily USDT volume

    "max_spread_enabled":       False,     # skip wide-spread pairs
    "max_spread_pct":           0.1,       # max bid/ask spread as % of mid price

    "cooldown_enabled":         False,     # wait after closing a trade on a pair
    "cooldown_candles":         4,         # hours to wait before re-entering same pair

    # ── Stop loss ──────────────────────────────────────────────────
    "atr_stop_mult":            2.0,       # stop = entry − (ATR × 2) — more breathing room

    "fixed_stop_enabled":       False,     # simple fixed % stop loss
    "fixed_stop_pct":           3.0,

    "trailing_stop_enabled":    True,      # rolling stop locks in profit as price rises
    "trailing_stop_pct":        2.5,       # trail 2.5% below the highest price since entry

    "breakeven_stop_enabled":   True,      # move stop to entry after TP1 — risk-free remainder

    # ── Take profit ────────────────────────────────────────────────
    "atr_tp1_mult":             2.5,       # TP1 = entry + (ATR × 2.5)
    "tp1_exit_pct":             50.0,      # exit 50% at TP1, trail the rest

    "fixed_tp_enabled":         False,
    "fixed_tp_pct":             5.0,

    "r_multiple_tp_enabled":    True,      # full target at 2.0× initial risk = 2:1 RR
    "r_multiple":               2.0,

    # ── Time stop ─────────────────────────────────────────────────
    "time_stop_candles":        24,        # exit losing trade after 24h (1 full day)

    # ── Risk controls ─────────────────────────────────────────────
    "daily_loss_limit_pct":     3.0,       # halt all trading if day loss exceeds 3%

    # ── Volume screener ────────────────────────────────────────────
    "screener_enabled":      False,    # auto-select pairs by volume instead of manual list
    "screener_top_n":        30,       # trade top N pairs by 24h USDT volume
    "screener_min_vol_usdt": 50000000, # skip pairs below $50M 24h volume
    "screener_exclude":      [],       # pairs to always skip even if in top N

    # ── Day trading strategy parameters ───────────────────────────
    "dt_price_rise_pct":        1.5,       # price must rise X% over lookback candles
    "dt_lookback_candles":      3,         # how many candles back to measure the rise
    "dt_volume_mult":           1.5,       # entry volume must be > N× 20-period avg
    "dt_max_rsi":               75.0,      # skip entry if RSI already overbought
    "dt_trailing_stop_pct":     1.5,       # trailing stop lags N% below price high
    "dt_take_profit_pct":       3.0,       # exit full position at N% gain
    "dt_breakeven_pct":         1.0,       # slide stop to entry once up N% (scratch losers)
    "dt_time_stop_candles":     20,        # close losing trade after N candles (capital protection)
}


# ── mtime-based cache: avoids disk reads when file hasn't changed ──────────

_cache: dict[str, tuple[float, dict]] = {}   # username -> (mtime, merged_rules)


def _rules_path(username: str) -> str:
    return os.path.join(_RULES_DIR, f"rules_{username}.json")


def load_rules(username: str = "default") -> dict:
    path = _rules_path(username)
    if os.path.exists(path):
        mtime = os.path.getmtime(path)
        cached = _cache.get(username)
        if cached and cached[0] == mtime:
            return cached[1].copy()
        with open(path) as f:
            saved = json.load(f)
        merged = {**DEFAULT_RULES, **saved}
        _cache[username] = (mtime, merged)
        return merged.copy()
    return DEFAULT_RULES.copy()


def save_rules(username: str, rules: dict) -> dict:
    merged = {**DEFAULT_RULES, **rules}
    path   = _rules_path(username)
    with open(path, "w") as f:
        json.dump(merged, f, indent=2)
    # Invalidate cache so next load_rules() reads from disk
    _cache.pop(username, None)
    return merged
