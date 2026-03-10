import os
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from src.exchange.client import BinanceClient
from src.bot.rules import load_rules, save_rules
from src.bot.engine import BotEngine
from src.ai.advisor import ai_recommend
from datetime import datetime

app = Flask(__name__)
CORS(app)

try:
    binance = BinanceClient()
except Exception as e:
    print(f"Warning: Binance client init failed: {e}")
    binance = None

engine = BotEngine(binance)


# ── Helpers ──────────────────────────────────────────────────────────────────

STABLECOIN_ASSETS = {"USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP"}
PRICE_CACHE = {}


def get_price(symbol: str) -> float:
    try:
        ticker = binance.get_ticker(symbol)
        return float(ticker["price"])
    except Exception:
        return 0.0


def estimate_usdt_value(asset: str, amount: float) -> float:
    if asset in STABLECOIN_ASSETS or asset == "USDT":
        return amount
    try:
        symbol = asset + "USDT"
        price = get_price(symbol)
        return amount * price if price else 0.0
    except Exception:
        return 0.0


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    if binance is None:
        return jsonify({
            "connected": False,
            "testnet": True,
            "server_time": None,
            "timestamp": datetime.utcnow().isoformat(),
            "bot": engine.get_stats(),
            "error": "Binance client unavailable (geo-restriction or bad keys)",
        })
    connected = binance.test_connection()
    server_time = binance.get_server_time() if connected else {}
    return jsonify({
        "connected": connected,
        "testnet": binance.testnet,
        "server_time": server_time.get("serverTime"),
        "timestamp": datetime.utcnow().isoformat(),
        "bot": engine.get_stats(),
    })


@app.route("/api/portfolio")
def api_portfolio():
    try:
        account = binance.get_account()
        raw_balances = account.get("balances", [])

        balances = []
        total_usdt = 0.0

        for b in raw_balances:
            free = float(b["free"])
            locked = float(b["locked"])
            total = free + locked
            if total < 0.0001:
                continue
            usdt_val = estimate_usdt_value(b["asset"], total)
            total_usdt += usdt_val
            balances.append({
                "asset": b["asset"],
                "free": free,
                "locked": locked,
                "total": total,
                "usdt_value": round(usdt_val, 2),
            })

        balances.sort(key=lambda x: x["usdt_value"], reverse=True)

        return jsonify({
            "balances": balances[:30],
            "total_usdt": round(total_usdt, 2),
            "account_type": account.get("accountType", "SPOT"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tickers")
def api_tickers():
    pairs = request.args.get("pairs", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT").split(",")
    results = []
    for pair in pairs:
        pair = pair.strip().upper()
        try:
            ticker = binance.client.get_ticker(symbol=pair)
            results.append({
                "symbol": pair,
                "price": float(ticker["lastPrice"]),
                "change_pct": float(ticker["priceChangePercent"]),
                "volume": float(ticker["volume"]),
                "high": float(ticker["highPrice"]),
                "low": float(ticker["lowPrice"]),
            })
        except Exception as e:
            results.append({"symbol": pair, "error": str(e)})
    return jsonify(results)


@app.route("/api/orders")
def api_orders():
    pairs = request.args.get("pairs", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT").split(",")
    all_orders = []
    for pair in pairs:
        try:
            orders = binance.client.get_all_orders(symbol=pair.strip().upper(), limit=10)
            all_orders.extend(orders)
        except Exception:
            pass
    all_orders.sort(key=lambda x: x.get("time", 0), reverse=True)
    return jsonify(all_orders[:50])


@app.route("/api/open-orders")
def api_open_orders():
    try:
        orders = binance.client.get_open_orders()
        return jsonify(orders)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rules", methods=["GET"])
def get_rules():
    return jsonify(load_rules())


@app.route("/api/rules", methods=["POST"])
def set_rules():
    data = request.json or {}
    saved = save_rules(data)
    engine._log("INFO", "Trading rules updated")
    return jsonify(saved)


@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    ok = engine.start()
    return jsonify({"started": ok, "running": engine.running})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    ok = engine.stop()
    return jsonify({"stopped": ok, "running": engine.running})


@app.route("/api/log")
def bot_log():
    limit = int(request.args.get("limit", 50))
    return jsonify(engine.get_log(limit))


@app.route("/api/positions")
def api_positions():
    return jsonify({
        "open":   engine.pm.get_open_positions(),
        "closed": engine.pm.get_closed_trades(50),
        "stats":  engine.pm.get_stats(),
    })


@app.route("/api/bot/stats")
def api_bot_stats():
    return jsonify(engine.get_stats())


@app.route("/api/ai-decide", methods=["POST"])
def ai_decide():
    if binance is None:
        return jsonify({"error": "Exchange not connected — cannot fetch market data"}), 400
    try:
        current_rules = load_rules()
        pairs         = current_rules.get("trade_pairs", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])
        rules, reasoning, market_assessment = ai_recommend(
            binance.client, pairs, current_rules
        )
        merged = save_rules({**current_rules, **rules})
        engine._log("INFO", f"AI decided: {market_assessment}")
        return jsonify({
            "rules":             merged,
            "reasoning":         reasoning,
            "market_assessment": market_assessment,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"AI call failed: {e}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
