"""
Strategy marketplace store — CRUD for user strategies and public listings.

Storage (all in /data on Railway, project root locally):
  strategies_{username}.json  — user's private strategy library
  marketplace_strategies.json — public marketplace listings
  user_credits.json           — credit balances keyed by username
"""

import json
import os
import uuid
from datetime import datetime

_DATA_DIR        = "/data" if os.path.isdir("/data") else os.path.abspath(
                       os.path.join(os.path.dirname(__file__), "../../"))
MARKETPLACE_FILE = os.path.join(_DATA_DIR, "marketplace_strategies.json")
CREDITS_FILE     = os.path.join(_DATA_DIR, "user_credits.json")
ACTIVE_FILE      = os.path.join(_DATA_DIR, "active_strategy.json")
STARTING_CREDITS = 500


# ── JSON helpers ───────────────────────────────────────────────────────────

def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _user_path(username: str) -> str:
    return os.path.join(_DATA_DIR, f"strategies_{username}.json")


# ── User strategies (private library) ──────────────────────────────────────

def get_my_strategies(username: str) -> list:
    return _load(_user_path(username), [])


def save_strategy(username: str, name: str, description: str,
                  rules: dict, backtest: dict | None = None) -> dict:
    strategies = get_my_strategies(username)
    strategy = {
        "id":          str(uuid.uuid4()),
        "name":        name,
        "description": description,
        "author":      username,
        "created_at":  datetime.utcnow().isoformat(),
        "rules":       rules,
        "backtest":    backtest,
        "is_public":   False,
        "price_credits": 0,
    }
    strategies.insert(0, strategy)
    _save(_user_path(username), strategies)
    return strategy


def update_strategy_backtest(username: str, strategy_id: str,
                             backtest: dict) -> dict | None:
    strategies = get_my_strategies(username)
    for s in strategies:
        if s["id"] == strategy_id:
            s["backtest"] = backtest
            _save(_user_path(username), strategies)
            return s
    return None


def delete_strategy(username: str, strategy_id: str) -> bool:
    strategies = get_my_strategies(username)
    before = len(strategies)
    strategies = [s for s in strategies if s["id"] != strategy_id]
    if len(strategies) < before:
        _save(_user_path(username), strategies)
        return True
    return False


# ── Marketplace (public listings) ──────────────────────────────────────────

def get_marketplace_listings() -> list:
    return _load(MARKETPLACE_FILE, [])


def publish_strategy(username: str, strategy_id: str,
                     price_credits: int = 0) -> tuple[dict | None, str | None]:
    strategies = get_my_strategies(username)
    strategy   = next((s for s in strategies if s["id"] == strategy_id), None)

    if not strategy:
        return None, "Strategy not found"
    if not strategy.get("backtest"):
        return None, "Backtest required before publishing — run a backtest first"

    marketplace = get_marketplace_listings()
    if any(s["id"] == strategy_id for s in marketplace):
        return None, "Already published to the marketplace"

    listing = {
        **strategy,
        "is_public":     True,
        "price_credits": max(0, int(price_credits)),
        "purchases":     0,
        "published_at":  datetime.utcnow().isoformat(),
    }
    marketplace.insert(0, listing)
    _save(MARKETPLACE_FILE, marketplace)

    # Mirror the public flag back to user's library
    for s in strategies:
        if s["id"] == strategy_id:
            s["is_public"]     = True
            s["price_credits"] = listing["price_credits"]
    _save(_user_path(username), strategies)

    return listing, None


def unpublish_strategy(username: str, strategy_id: str) -> bool:
    marketplace = get_marketplace_listings()
    listing     = next((s for s in marketplace if s["id"] == strategy_id), None)
    if not listing or listing["author"] != username:
        return False
    marketplace = [s for s in marketplace if s["id"] != strategy_id]
    _save(MARKETPLACE_FILE, marketplace)

    strategies = get_my_strategies(username)
    for s in strategies:
        if s["id"] == strategy_id:
            s["is_public"] = False
    _save(_user_path(username), strategies)
    return True


# ── Credits ────────────────────────────────────────────────────────────────

def get_credits(username: str) -> int:
    credits = _load(CREDITS_FILE, {})
    if username not in credits:
        credits[username] = STARTING_CREDITS
        _save(CREDITS_FILE, credits)
    return int(credits[username])


def _adjust_credits(username: str, delta: int) -> int:
    credits = _load(CREDITS_FILE, {})
    current = int(credits.get(username, STARTING_CREDITS))
    credits[username] = max(0, current + delta)
    _save(CREDITS_FILE, credits)
    return credits[username]


# ── Purchasing ─────────────────────────────────────────────────────────────

def purchase_strategy(buyer: str, strategy_id: str) -> tuple[dict | None, str | None]:
    marketplace = get_marketplace_listings()
    listing     = next((s for s in marketplace if s["id"] == strategy_id), None)

    if not listing:
        return None, "Strategy not found"
    if listing["author"] == buyer:
        return None, "You cannot purchase your own strategy"

    # Check buyer doesn't already own it
    for s in get_my_strategies(buyer):
        if s.get("source_id") == strategy_id or s["id"] == strategy_id:
            return None, "You already own this strategy"

    price = int(listing.get("price_credits", 0))
    if get_credits(buyer) < price:
        return None, f"Insufficient credits — you need {price}, you have {get_credits(buyer)}"

    _adjust_credits(buyer, -price)
    if price > 0:
        _adjust_credits(listing["author"], price)

    # Clone into buyer's library
    copy = {
        **listing,
        "id":              str(uuid.uuid4()),
        "source_id":       strategy_id,
        "purchased_from":  listing["author"],
        "purchased_at":    datetime.utcnow().isoformat(),
        "is_public":       False,
    }
    buyer_strategies = get_my_strategies(buyer)
    buyer_strategies.insert(0, copy)
    _save(_user_path(buyer), buyer_strategies)

    # Increment purchase counter
    for s in marketplace:
        if s["id"] == strategy_id:
            s["purchases"] = s.get("purchases", 0) + 1
    _save(MARKETPLACE_FILE, marketplace)

    return copy, None


def get_strategy_rules(username: str, strategy_id: str) -> dict | None:
    """Retrieve rules for a strategy the user owns."""
    for s in get_my_strategies(username):
        if s["id"] == strategy_id:
            return s.get("rules")
    return None


# ── Active loaded strategy (dashboard indicator) ───────────────────────────

def _active_store() -> dict:
    return _load(ACTIVE_FILE, {})


def get_active_strategy(username: str) -> dict | None:
    """Return metadata for the currently loaded marketplace strategy, or None."""
    return _active_store().get(username)


def set_active_strategy(username: str, strategy_id: str, name: str, author: str):
    data = _active_store()
    data[username] = {
        "id":        strategy_id,
        "name":      name,
        "author":    author,
        "loaded_at": datetime.utcnow().isoformat(),
    }
    _save(ACTIVE_FILE, data)


def clear_active_strategy(username: str):
    data = _active_store()
    if username in data:
        del data[username]
        _save(ACTIVE_FILE, data)
