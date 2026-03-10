"""
Auth & API key manager.
Credentials and keys are stored in config.json (project root).
Environment variables serve as fallbacks (useful for Railway).
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
    return {"users": [], "api_keys": {}}


def save_config(config: dict):
    with open(_path(), "w") as f:
        json.dump(config, f, indent=2)


# ── Setup / user management ─────────────────────────────────────────

def is_setup_complete() -> bool:
    """True if at least one login method is configured."""
    if os.environ.get("ADMIN_USERNAME") and os.environ.get("ADMIN_PASSWORD"):
        return True
    return bool(load_config().get("users"))


def setup_user(username: str, password: str):
    config = load_config()
    config["users"] = [{"username": username, "password_hash": generate_password_hash(password)}]
    save_config(config)


def verify_credentials(username: str, password: str) -> bool:
    # Env-var credentials (Railway)
    env_user = os.environ.get("ADMIN_USERNAME")
    env_pass = os.environ.get("ADMIN_PASSWORD")
    if env_user and env_pass:
        return username == env_user and password == env_pass
    # Stored credentials
    for user in load_config().get("users", []):
        if user["username"] == username:
            return check_password_hash(user["password_hash"], password)
    return False


def get_username() -> str:
    if os.environ.get("ADMIN_USERNAME"):
        return os.environ["ADMIN_USERNAME"]
    users = load_config().get("users", [])
    return users[0]["username"] if users else "admin"


def change_password(new_password: str):
    config = load_config()
    if not config.get("users"):
        return
    config["users"][0]["password_hash"] = generate_password_hash(new_password)
    save_config(config)


# ── API keys ────────────────────────────────────────────────────────

def get_api_keys() -> dict:
    """config.json takes priority; env vars are the fallback."""
    stored = load_config().get("api_keys", {})
    return {
        "binance_api_key":    stored.get("binance_api_key")    or os.environ.get("BINANCE_TESTNET_API_KEY", ""),
        "binance_secret_key": stored.get("binance_secret_key") or os.environ.get("BINANCE_TESTNET_SECRET_KEY", ""),
        "anthropic_api_key":  stored.get("anthropic_api_key")  or os.environ.get("ANTHROPIC_API_KEY", ""),
        "use_testnet":        stored.get("use_testnet", os.environ.get("USE_TESTNET", "true").lower() == "true"),
    }


def save_api_keys(keys: dict):
    config = load_config()
    config["api_keys"] = keys
    save_config(config)


def mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "•" * len(s)
    return s[:4] + "•" * (len(s) - 8) + s[-4:]
