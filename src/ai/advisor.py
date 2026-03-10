"""
AI market advisor — calls Claude to analyse live indicator data
and recommend optimal strategy parameters for current conditions.
"""

import os
import json
import anthropic

from src.bot.indicators import klines_to_df, compute_indicators


# ── Market data collection ──────────────────────────────────────────

def _gather_market_data(client, pairs: list) -> list:
    data = []
    for symbol in pairs[:6]:
        try:
            raw = client.get_klines(symbol=symbol, interval="1h", limit=100)
            df  = compute_indicators(klines_to_df(raw))
            c   = df.iloc[-2]
            c1  = df.iloc[-3]
            tk  = client.get_ticker(symbol=symbol)
            data.append({
                "symbol":            symbol,
                "price":             round(float(tk.get("lastPrice", 0)), 4),
                "change_24h_pct":    round(float(tk.get("priceChangePercent", 0)), 2),
                "volume_24h_usdt":   round(float(tk.get("quoteVolume", 0))),
                "rsi":               round(float(c["rsi"]),  1),
                "rsi_prev":          round(float(c1["rsi"]), 1),
                "adx":               round(float(c["adx"]),  1),
                "trend_aligned":     bool(c["ema20"] > c["ema50"] > c["ema200"]),
                "price_above_ema50": bool(float(c["close"]) > float(c["ema50"])),
                "atr_pct":           round(float(c["atr"]) / float(c["close"]) * 100, 3),
                "macd_hist":         round(float(c["macd_hist"]), 6),
                "macd_rising":       bool(c["macd_hist"] > c1["macd_hist"]),
                "volume_ratio":      round(float(c["volume_ratio"]), 2),
                "body_pct":          round(float(c["body_pct"]), 1),
            })
        except Exception as e:
            data.append({"symbol": symbol, "error": str(e)})
    return data


# ── Tool schema ─────────────────────────────────────────────────────

_STRATEGY_TOOL = {
    "name": "set_strategy",
    "description": (
        "Configure the trading bot strategy parameters based on live market analysis. "
        "Return ALL fields — they will be applied directly to the bot."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "market_assessment": {
                "type": "string",
                "description": "One sentence describing current market conditions (trending/ranging, volatility level, dominant direction)."
            },
            "reasoning": {
                "type": "string",
                "description": "2-3 sentences explaining why these specific settings were chosen given the data."
            },
            "rules": {
                "type": "object",
                "properties": {
                    "interval":                {"type": "string",  "enum": ["1m","5m","15m","30m","1h","4h","1d"]},
                    "rsi_enabled":             {"type": "boolean"},
                    "rsi_dip_low":             {"type": "number"},
                    "rsi_dip_high":            {"type": "number"},
                    "rsi_cross_level":         {"type": "number"},
                    "adx_enabled":             {"type": "boolean"},
                    "adx_min":                 {"type": "number"},
                    "volume_spike_enabled":    {"type": "boolean"},
                    "volume_spike_mult":       {"type": "number"},
                    "macd_filter_enabled":     {"type": "boolean"},
                    "macd_mode":               {"type": "string", "enum": ["positive","turning_up"]},
                    "body_filter_enabled":     {"type": "boolean"},
                    "body_filter_pct":         {"type": "number"},
                    "min_volume_usdt_enabled": {"type": "boolean"},
                    "min_volume_usdt":         {"type": "number"},
                    "max_spread_enabled":      {"type": "boolean"},
                    "max_spread_pct":          {"type": "number"},
                    "cooldown_enabled":        {"type": "boolean"},
                    "cooldown_candles":        {"type": "integer"},
                    "atr_stop_mult":           {"type": "number"},
                    "fixed_stop_enabled":      {"type": "boolean"},
                    "fixed_stop_pct":          {"type": "number"},
                    "trailing_stop_enabled":   {"type": "boolean"},
                    "trailing_stop_pct":       {"type": "number"},
                    "breakeven_stop_enabled":  {"type": "boolean"},
                    "atr_tp1_mult":            {"type": "number"},
                    "tp1_exit_pct":            {"type": "number"},
                    "fixed_tp_enabled":        {"type": "boolean"},
                    "fixed_tp_pct":            {"type": "number"},
                    "r_multiple_tp_enabled":   {"type": "boolean"},
                    "r_multiple":              {"type": "number"},
                    "time_stop_candles":       {"type": "integer"},
                    "risk_per_trade_pct":      {"type": "number"},
                    "max_open_positions":      {"type": "integer"},
                    "daily_loss_limit_pct":    {"type": "number"},
                },
                "required": [
                    "interval", "rsi_enabled", "adx_enabled",
                    "atr_stop_mult", "atr_tp1_mult", "tp1_exit_pct",
                    "time_stop_candles", "risk_per_trade_pct",
                    "max_open_positions", "daily_loss_limit_pct",
                ]
            }
        },
        "required": ["market_assessment", "reasoning", "rules"]
    }
}


# ── Main entry point ────────────────────────────────────────────────

def ai_recommend(binance_client, pairs: list, current_rules: dict) -> tuple:
    """
    Analyse market conditions and recommend strategy settings.
    Returns (rules_dict, reasoning, market_assessment).
    Raises ValueError on missing API key or failed call.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")

    market_data = _gather_market_data(binance_client, pairs)

    system = """You are an expert quantitative crypto trading analyst.
You are configuring an automated RSI Momentum + EMA Trend Filter bot trading on Binance.
Starting capital: $100 USDT. The EMA trend stack check (20>50>200) is always active.

Calibration guidelines:
- Strong trending market (most pairs trend-aligned, ADX>25): relax RSI thresholds (dip_low 38-44), use trailing stops, increase ATR TP multiplier, raise max positions.
- Choppy/ranging (few aligned, ADX<20): tighten RSI zone (dip_low 44-50), add MACD+volume filters, enable cooldown, reduce max positions to 1-2.
- High volatility (ATR%>2%): widen ATR stop multiplier (2.0-3.0), lower risk per trade (0.5%), tighten daily loss limit.
- Low volatility (ATR%<0.5%): tighter stops (1.0-1.5x), can increase risk slightly.
- Always keep daily_loss_limit_pct conservative (2-4%).
- Prefer 1h interval unless there's a clear reason to go shorter/longer.
- Trade pairs list is not changed by your settings — focus on the parameters only."""

    prompt = f"""Live market data (1H candles, last closed candle):
{json.dumps(market_data, indent=2)}

Current bot configuration (for context):
{json.dumps(current_rules, indent=2)}

Analyse the above and call set_strategy with your full recommended configuration."""

    client   = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model        = "claude-sonnet-4-6",
        max_tokens   = 2048,
        system       = system,
        tools        = [_STRATEGY_TOOL],
        tool_choice  = {"type": "tool", "name": "set_strategy"},
        messages     = [{"role": "user", "content": prompt}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "set_strategy":
            inp = block.input
            return inp["rules"], inp["reasoning"], inp["market_assessment"]

    raise ValueError("AI returned no strategy recommendation")
