import json
import os

RULES_FILE = os.path.join(os.path.dirname(__file__), "../../rules.json")

DEFAULT_RULES = {
    "take_profit_pct": 5.0,
    "stop_loss_pct": 3.0,
    "max_position_size_pct": 10.0,
    "trade_pairs": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"],
    "order_type": "MARKET",
    "min_volume_usdt": 100.0,
    "cooldown_seconds": 60,
    "trailing_stop": False,
    "trailing_stop_pct": 1.5,
    "max_open_positions": 3,
    "quote_asset": "USDT",
}


def load_rules() -> dict:
    path = os.path.abspath(RULES_FILE)
    if os.path.exists(path):
        with open(path) as f:
            saved = json.load(f)
        rules = {**DEFAULT_RULES, **saved}
    else:
        rules = DEFAULT_RULES.copy()
    return rules


def save_rules(rules: dict) -> dict:
    merged = {**DEFAULT_RULES, **rules}
    path = os.path.abspath(RULES_FILE)
    with open(path, "w") as f:
        json.dump(merged, f, indent=2)
    return merged
