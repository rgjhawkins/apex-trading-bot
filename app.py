import os
from flask import Flask, jsonify, request, render_template, session, redirect
from flask_cors import CORS
from datetime import datetime

from src.exchange.client import BinanceClient
from src.bot.rules import load_rules, save_rules
from src.bot.engine import BotEngine
from src.ai.advisor import ai_recommend
from src.auth.manager import (
    is_username_taken, register_user, verify_credentials,
    change_password, get_api_keys, save_api_keys, mask,
)

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "apex-change-this-in-production")


# ── Seed user from env vars (survives Railway redeploys) ─────────────

def _seed_user_from_env():
    """
    If APEX_USERNAME + APEX_PASSWORD are set, ensure that user exists in
    config.json (creates them on a fresh deploy, skips if already present).
    API keys are also seeded from env vars if provided.
    """
    username = os.environ.get("APEX_USERNAME", "").strip()
    password = os.environ.get("APEX_PASSWORD", "").strip()
    if not username or not password:
        return
    if is_username_taken(username):
        return
    email = os.environ.get("APEX_EMAIL", "").strip()
    register_user(username, password, email)
    keys = {
        "binance_api_key":    os.environ.get("APEX_BINANCE_API_KEY",    ""),
        "binance_secret_key": os.environ.get("APEX_BINANCE_SECRET_KEY", ""),
        "anthropic_api_key":  os.environ.get("APEX_ANTHROPIC_API_KEY",  ""),
        "use_testnet":        os.environ.get("APEX_USE_TESTNET", "true").lower() != "false",
    }
    save_api_keys(username, keys)
    print(f"[startup] Seeded user '{username}' from environment variables")

_seed_user_from_env()


# ── Per-user engine registry ──────────────────────────────────────

_engines: dict[str, BotEngine] = {}
_clients: dict[str, BinanceClient | None] = {}


def _make_client(username: str) -> BinanceClient | None:
    keys = get_api_keys(username)
    if not keys["binance_api_key"]:
        return None
    if keys.get("anthropic_api_key"):
        os.environ["ANTHROPIC_API_KEY"] = keys["anthropic_api_key"]
    try:
        return BinanceClient(
            api_key    = keys["binance_api_key"],
            secret_key = keys["binance_secret_key"],
            testnet    = keys["use_testnet"],
        )
    except Exception as e:
        print(f"[{username}] Binance client init failed: {e}")
        return None


def get_engine(username: str) -> BotEngine:
    """Return the BotEngine for this user, creating it on first access."""
    if username not in _engines:
        client = _make_client(username)
        _clients[username] = client
        _engines[username] = BotEngine(client, username=username)
    return _engines[username]


def reinit_client(username: str, api_key: str, secret_key: str,
                  anthropic_key: str, use_testnet: bool):
    """Rebuild the Binance client for a user (stops bot first if running)."""
    if anthropic_key:
        os.environ["ANTHROPIC_API_KEY"] = anthropic_key
    eng = get_engine(username)
    if eng.running:
        eng.stop()
    try:
        client = BinanceClient(api_key=api_key, secret_key=secret_key, testnet=use_testnet)
        _clients[username] = client
        eng.client = client
        eng._log("INFO", f"Exchange client reconnected ({'testnet' if use_testnet else 'live'})")
    except Exception as e:
        _clients[username] = None
        eng.client = None
        raise ValueError(str(e))


# ── Auth middleware ───────────────────────────────────────────────

_OPEN_PATHS = ("/login", "/register")

@app.before_request
def require_login():
    if request.path.startswith(_OPEN_PATHS) or request.path.startswith("/static"):
        return None
    if not session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not authenticated"}), 401
        return redirect("/login")


# ── Auth routes ───────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if verify_credentials(username, password):
            session["logged_in"] = True
            session["username"]  = username
            return redirect("/")
        return render_template("login.html", error="Invalid username or password")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm",  "")
        if not username or not password:
            error = "Username and password are required"
        elif len(username) < 3:
            error = "Username must be at least 3 characters"
        elif not username.isalnum():
            error = "Username may only contain letters and numbers"
        elif is_username_taken(username):
            error = "That username is already taken"
        elif password != confirm:
            error = "Passwords do not match"
        elif len(password) < 8:
            error = "Password must be at least 8 characters"
        else:
            email = request.form.get("email", "").strip()
            register_user(username, password, email)
            keys = {
                "binance_api_key":    request.form.get("binance_api_key",    "").strip(),
                "binance_secret_key": request.form.get("binance_secret_key", "").strip(),
                "anthropic_api_key":  request.form.get("anthropic_api_key",  "").strip(),
                "use_testnet":        request.form.get("use_testnet") == "on",
            }
            save_api_keys(username, keys)
            try:
                reinit_client(username, keys["binance_api_key"], keys["binance_secret_key"],
                              keys["anthropic_api_key"], keys["use_testnet"])
            except Exception:
                pass
            return redirect("/login")
    return render_template("setup.html", error=error)


# ── Helpers ───────────────────────────────────────────────────────

STABLECOIN_ASSETS = {"USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP"}


def get_price(client: BinanceClient, symbol: str) -> float:
    try:
        return float(client.get_ticker(symbol)["price"])
    except Exception:
        return 0.0


def estimate_usdt_value(client: BinanceClient, asset: str, amount: float) -> float:
    if asset in STABLECOIN_ASSETS or asset == "USDT":
        return amount
    try:
        return amount * (get_price(client, asset + "USDT") or 0.0)
    except Exception:
        return 0.0


# ── Main routes ───────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", username=session.get("username", ""))


@app.route("/api/status")
def api_status():
    username = session.get("username", "")
    eng      = get_engine(username)
    client   = _clients.get(username)
    if client is None:
        return jsonify({
            "connected": False, "testnet": True,
            "server_time": None, "timestamp": datetime.utcnow().isoformat(),
            "bot": eng.get_stats(),
            "error": "Binance client unavailable — add API keys in Settings",
        })
    try:
        connected   = client.test_connection()
        server_time = client.get_server_time() if connected else {}
    except Exception as e:
        return jsonify({
            "connected": False, "testnet": client.testnet,
            "server_time": None, "timestamp": datetime.utcnow().isoformat(),
            "bot": eng.get_stats(),
            "error": str(e),
        })
    return jsonify({
        "connected":   connected,
        "testnet":     client.testnet,
        "server_time": server_time.get("serverTime"),
        "timestamp":   datetime.utcnow().isoformat(),
        "bot":         eng.get_stats(),
    })


@app.route("/api/portfolio")
def api_portfolio():
    username = session.get("username", "")
    client   = _clients.get(username)
    if client is None:
        return jsonify({"error": "Exchange not connected"}), 400
    try:
        account      = client.get_account()
        raw_balances = account.get("balances", [])
        balances, total_usdt = [], 0.0
        for b in raw_balances:
            free  = float(b["free"])
            total = free + float(b["locked"])
            if total < 0.0001:
                continue
            usdt_val = estimate_usdt_value(client, b["asset"], total)
            total_usdt += usdt_val
            balances.append({"asset": b["asset"], "free": free,
                              "total": total, "usdt_value": round(usdt_val, 2)})
        balances.sort(key=lambda x: x["usdt_value"], reverse=True)
        return jsonify({"balances": balances[:30], "total_usdt": round(total_usdt, 2),
                        "account_type": account.get("accountType", "SPOT")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tickers")
def api_tickers():
    username = session.get("username", "")
    client   = _clients.get(username)
    if client is None:
        return jsonify([])
    pairs   = request.args.get("pairs", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT").split(",")
    results = []
    for pair in pairs:
        pair = pair.strip().upper()
        try:
            t = client.client.get_ticker(symbol=pair)
            results.append({"symbol": pair, "price": float(t["lastPrice"]),
                            "change_pct": float(t["priceChangePercent"]),
                            "volume": float(t["volume"]),
                            "high": float(t["highPrice"]), "low": float(t["lowPrice"])})
        except Exception as e:
            results.append({"symbol": pair, "error": str(e)})
    return jsonify(results)


@app.route("/api/orders")
def api_orders():
    username = session.get("username", "")
    client   = _clients.get(username)
    if client is None:
        return jsonify([])
    pairs      = request.args.get("pairs", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT").split(",")
    all_orders = []
    for pair in pairs:
        try:
            all_orders.extend(client.client.get_all_orders(symbol=pair.strip().upper(), limit=10))
        except Exception:
            pass
    all_orders.sort(key=lambda x: x.get("time", 0), reverse=True)
    return jsonify(all_orders[:50])


@app.route("/api/open-orders")
def api_open_orders():
    username = session.get("username", "")
    client   = _clients.get(username)
    if client is None:
        return jsonify([])
    try:
        return jsonify(client.client.get_open_orders())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rules", methods=["GET"])
def get_rules():
    username = session.get("username", "")
    return jsonify(load_rules(username))


_RULE_LABELS = {
    "rsi_enabled":            "RSI filter",
    "rsi_dip_low":            "RSI dip low",
    "rsi_dip_high":           "RSI dip high",
    "rsi_cross_level":        "RSI cross level",
    "adx_enabled":            "ADX filter",
    "adx_min":                "ADX minimum",
    "volume_spike_enabled":   "Volume spike",
    "volume_spike_mult":      "Volume spike mult",
    "macd_filter_enabled":    "MACD filter",
    "macd_mode":              "MACD mode",
    "body_filter_enabled":    "Candle body filter",
    "body_filter_pct":        "Body filter %",
    "min_volume_usdt_enabled":"Min volume filter",
    "min_volume_usdt":        "Min volume (USDT)",
    "max_spread_enabled":     "Spread filter",
    "max_spread_pct":         "Max spread %",
    "cooldown_enabled":       "Re-entry cooldown",
    "cooldown_candles":       "Cooldown hours",
    "atr_stop_mult":          "ATR stop mult",
    "fixed_stop_enabled":     "Fixed stop",
    "fixed_stop_pct":         "Fixed stop %",
    "trailing_stop_enabled":  "Trailing stop",
    "trailing_stop_pct":      "Trailing stop %",
    "breakeven_stop_enabled": "Breakeven stop",
    "atr_tp1_mult":           "ATR TP1 mult",
    "tp1_exit_pct":           "TP1 exit %",
    "fixed_tp_enabled":       "Fixed TP",
    "fixed_tp_pct":           "Fixed TP %",
    "r_multiple_tp_enabled":  "R-Multiple TP",
    "r_multiple":             "R-Multiple",
    "time_stop_candles":      "Time stop (h)",
    "risk_per_trade_pct":     "Risk per trade %",
    "max_open_positions":     "Max positions",
    "daily_loss_limit_pct":   "Daily loss limit %",
    "trade_pairs":            "Trade pairs",
    "interval":               "Interval",
    "strategy":               "Strategy",
    "dt_price_rise_pct":      "DT price rise %",
    "dt_lookback_candles":    "DT lookback candles",
    "dt_volume_mult":         "DT volume mult",
    "dt_max_rsi":             "DT max RSI",
    "dt_trailing_stop_pct":   "DT trailing stop %",
    "dt_take_profit_pct":     "DT take profit %",
    "dt_breakeven_pct":       "DT breakeven %",
    "dt_time_stop_candles":   "DT time stop candles",
}

def _fmt(val):
    if isinstance(val, bool):
        return "ON" if val else "OFF"
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val)


@app.route("/api/rules", methods=["POST"])
def set_rules():
    username = session.get("username", "")
    current  = load_rules(username)
    saved    = save_rules(username, request.json or {})
    eng      = get_engine(username)

    changes = [
        f"{_RULE_LABELS[k]}: {_fmt(current.get(k))} → {_fmt(saved.get(k))}"
        for k in _RULE_LABELS
        if current.get(k) != saved.get(k)
    ]
    if changes:
        eng._log("INFO", f"Rules updated — {len(changes)} change{'s' if len(changes) > 1 else ''}")
        for change in changes:
            eng._log("INFO", f"  ↳ {change}")
    else:
        eng._log("INFO", "Rules saved (no changes)")

    return jsonify({"rules": saved, "changes": changes})


@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    username = session.get("username", "")
    eng = get_engine(username)
    ok  = eng.start()
    return jsonify({"started": ok, "running": eng.running})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    username = session.get("username", "")
    eng = get_engine(username)
    ok  = eng.stop()
    return jsonify({"stopped": ok, "running": eng.running})


@app.route("/api/log")
def bot_log():
    username = session.get("username", "")
    limit    = int(request.args.get("limit", 50))
    return jsonify(get_engine(username).get_log(limit))


@app.route("/api/positions")
def api_positions():
    username = session.get("username", "")
    eng      = get_engine(username)
    return jsonify({
        "open":   eng.pm.get_open_positions(),
        "closed": eng.pm.get_closed_trades(50),
        "stats":  eng.pm.get_stats(),
    })


@app.route("/api/bot/stats")
def api_bot_stats():
    username = session.get("username", "")
    return jsonify(get_engine(username).get_stats())


@app.route("/api/ai-decide", methods=["POST"])
def ai_decide():
    username = session.get("username", "")
    client   = _clients.get(username)
    if client is None:
        return jsonify({"error": "Exchange not connected — cannot fetch market data"}), 400
    try:
        current_rules = load_rules(username)
        pairs         = current_rules.get("trade_pairs", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])
        rules, reasoning, market_assessment = ai_recommend(client.client, pairs, current_rules)
        merged = save_rules(username, {**current_rules, **rules})
        get_engine(username)._log("INFO", f"AI decided: {market_assessment}")
        return jsonify({"rules": merged, "reasoning": reasoning, "market_assessment": market_assessment})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"AI call failed: {e}"}), 500


# ── API Keys management ───────────────────────────────────────────

@app.route("/api/keys", methods=["GET"])
def api_get_keys():
    username = session.get("username", "")
    keys     = get_api_keys(username)
    return jsonify({
        "binance_api_key":    mask(keys["binance_api_key"]),
        "binance_secret_key": mask(keys["binance_secret_key"]),
        "anthropic_api_key":  mask(keys["anthropic_api_key"]),
        "use_testnet":        keys["use_testnet"],
        "has_binance":        bool(keys["binance_api_key"]),
        "has_anthropic":      bool(keys["anthropic_api_key"]),
    })


@app.route("/api/keys", methods=["POST"])
def api_update_keys():
    username = session.get("username", "")
    data     = request.json or {}
    current  = get_api_keys(username)

    def resolve(field, current_val):
        val = data.get(field, "")
        return val if val and "•" not in val else current_val

    new_keys = {
        "binance_api_key":    resolve("binance_api_key",    current["binance_api_key"]),
        "binance_secret_key": resolve("binance_secret_key", current["binance_secret_key"]),
        "anthropic_api_key":  resolve("anthropic_api_key",  current["anthropic_api_key"]),
        "use_testnet":        data.get("use_testnet", current["use_testnet"]),
    }
    save_api_keys(username, new_keys)
    try:
        reinit_client(username, new_keys["binance_api_key"], new_keys["binance_secret_key"],
                      new_keys["anthropic_api_key"], new_keys["use_testnet"])
        return jsonify({"ok": True, "message": "Keys saved and exchange reconnected"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/password", methods=["POST"])
def api_change_password():
    username   = session.get("username", "")
    data       = request.json or {}
    current_pw = data.get("current_password", "")
    new_pw     = data.get("new_password", "")
    if not verify_credentials(username, current_pw):
        return jsonify({"error": "Current password is incorrect"}), 400
    if len(new_pw) < 8:
        return jsonify({"error": "New password must be at least 8 characters"}), 400
    change_password(username, new_pw)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
