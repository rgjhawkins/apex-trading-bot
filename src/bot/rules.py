import json
import os

RULES_FILE = os.path.join(os.path.dirname(__file__), "../../rules.json")

DEFAULT_RULES = {
    # ── Pairs & execution ──────────────────────────────────────────
    "trade_pairs":       ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"],
    "max_open_positions": 3,
    "order_type":        "MARKET",

    # ── Position sizing ────────────────────────────────────────────
    "risk_per_trade_pct": 1.0,       # % of starting capital risked per trade

    # ── Entry filters ──────────────────────────────────────────────
    "rsi_dip_low":    42.0,          # RSI must dip below this before recovery
    "rsi_dip_high":   55.0,          # RSI dip upper bound
    "rsi_cross_level": 50.0,         # RSI must cross back above this to trigger entry
    "adx_min":        20.0,          # minimum ADX — skip flat/choppy markets

    # ── Stop loss & take profit (ATR-based multipliers) ────────────
    "atr_stop_mult":  1.5,           # stop = entry - (ATR × this)
    "atr_tp1_mult":   1.5,           # TP1  = entry + (ATR × this)
    "tp1_exit_pct":   40.0,          # % of position to close at TP1

    # ── Time stop ─────────────────────────────────────────────────
    "time_stop_candles": 20,         # exit losing position after N 1H candles

    # ── Risk controls ─────────────────────────────────────────────
    "daily_loss_limit_pct": 3.0,     # halt trading if day loss exceeds this %
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
