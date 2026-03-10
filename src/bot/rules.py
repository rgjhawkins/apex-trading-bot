import json
import os

RULES_FILE = os.path.join(os.path.dirname(__file__), "../../rules.json")

DEFAULT_RULES = {
    # ── Pairs & execution ──────────────────────────────────────────
    "trade_pairs":              ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"],
    "max_open_positions":       3,

    # ── Position sizing ────────────────────────────────────────────
    "risk_per_trade_pct":       1.0,       # % of $100 capital risked per trade

    # ── Entry filters ──────────────────────────────────────────────
    "rsi_dip_low":              42.0,
    "rsi_dip_high":             55.0,
    "rsi_cross_level":          50.0,
    "adx_min":                  20.0,

    "volume_spike_enabled":     False,     # require volume spike on entry candle
    "volume_spike_mult":        1.5,       # entry volume must be > N× 20-period average

    "macd_filter_enabled":      False,     # require MACD confirmation
    "macd_mode":                "positive",# "positive" = histogram > 0, "turning_up" = rising

    "body_filter_enabled":      False,     # require strong candle body (no dojis)
    "body_filter_pct":          50.0,      # candle body must be > X% of high-low range

    "min_volume_usdt_enabled":  False,     # skip low-volume pairs
    "min_volume_usdt":          5000000.0, # minimum $5M daily USDT volume

    "max_spread_enabled":       False,     # skip wide-spread pairs
    "max_spread_pct":           0.1,       # max bid/ask spread as % of mid price

    "cooldown_enabled":         False,     # wait after closing a trade on a pair
    "cooldown_candles":         3,         # number of 1H candles to wait before re-entering

    # ── Stop loss ──────────────────────────────────────────────────
    "atr_stop_mult":            1.5,       # stop = entry − (ATR × this)  [always active]

    "fixed_stop_enabled":       False,     # simple fixed % stop loss
    "fixed_stop_pct":           3.0,       # stop at X% below entry price

    "trailing_stop_enabled":    False,     # rolling stop that follows price up
    "trailing_stop_pct":        2.0,       # trail X% below the highest price since entry

    "breakeven_stop_enabled":   False,     # move stop to entry price after TP1 is hit

    # ── Take profit ────────────────────────────────────────────────
    "atr_tp1_mult":             1.5,       # TP1 = entry + (ATR × this)  [default]
    "tp1_exit_pct":             40.0,      # % of position to close at TP1

    "fixed_tp_enabled":         False,     # simple fixed % take profit
    "fixed_tp_pct":             5.0,       # take profit at X% above entry price

    "r_multiple_tp_enabled":    False,     # take profit at N× the initial risk
    "r_multiple":               2.0,       # e.g. 2.0 = exit when profit = 2× the stop distance

    # ── Time stop ─────────────────────────────────────────────────
    "time_stop_candles":        20,        # exit losing trade after N 1H candles

    # ── Risk controls ─────────────────────────────────────────────
    "daily_loss_limit_pct":     3.0,       # halt all trading if day loss exceeds this %
}


def load_rules() -> dict:
    path = os.path.abspath(RULES_FILE)
    if os.path.exists(path):
        with open(path) as f:
            saved = json.load(f)
        return {**DEFAULT_RULES, **saved}
    return DEFAULT_RULES.copy()


def save_rules(rules: dict) -> dict:
    merged = {**DEFAULT_RULES, **rules}
    path = os.path.abspath(RULES_FILE)
    with open(path, "w") as f:
        json.dump(merged, f, indent=2)
    return merged
