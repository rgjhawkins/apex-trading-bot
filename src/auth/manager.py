"""
Auth & API key manager — multi-user, no special admin.
All credentials and keys are stored per-user in config.json.
"""

import json
import os
from werkzeug.security import generate_password_hash, check_password_hash

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "../../config.json")


def _path() -> str:
    return os.path.abspath(CONFIG_FILE)


def load_config() -> dict:
    try:
        if os.path.exists(_path()):
            with open(_path()) as f:
                return json.load(f)
    except Exception:
        pass
    return {"users": []}


def save_config(config: dict):
    with open(_path(), "w") as f:
        json.dump(config, f, indent=2)


# ── Registration ───────────────────────────────────────────────────

def is_username_taken(username: str) -> bool:
    return any(u["username"] == username for u in load_config().get("users", []))


def register_user(username: str, password: str, email: str = ""):
    config = load_config()
    config.setdefault("users", []).append({
        "username":      username,
        "password_hash": generate_password_hash(password),
        "email":         email,
        "api_keys": {
            "binance_api_key":    "",
            "binance_secret_key": "",
            "anthropic_api_key":  "",
            "use_testnet":        True,
        },
    })
    save_config(config)


# ── Auth ───────────────────────────────────────────────────────────

def verify_credentials(username: str, password: str) -> bool:
    for user in load_config().get("users", []):
        if user["username"] == username:
            return check_password_hash(user["password_hash"], password)
    return False


def change_password(username: str, new_password: str):
    config = load_config()
    for user in config.get("users", []):
        if user["username"] == username:
            user["password_hash"] = generate_password_hash(new_password)
            save_config(config)
            return


# ── API keys (per-user) ────────────────────────────────────────────

def get_api_keys(username: str) -> dict:
    for user in load_config().get("users", []):
        if user["username"] == username:
            k = user.get("api_keys", {})
            return {
                "binance_api_key":    k.get("binance_api_key", ""),
                "binance_secret_key": k.get("binance_secret_key", ""),
                "anthropic_api_key":  k.get("anthropic_api_key", ""),
                "use_testnet":        k.get("use_testnet", True),
            }
    return {"binance_api_key": "", "binance_secret_key": "", "anthropic_api_key": "", "use_testnet": True}


def save_api_keys(username: str, keys: dict):
    config = load_config()
    for user in config.get("users", []):
        if user["username"] == username:
            user["api_keys"] = keys
            save_config(config)
            return


# ── Helpers ────────────────────────────────────────────────────────

def mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "•" * len(s)
    return s[:4] + "•" * (len(s) - 8) + s[-4:]
