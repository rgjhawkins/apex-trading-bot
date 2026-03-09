"""
Technical indicator calculations from raw Binance kline data.
All signals are generated on CLOSED candles only (index -2, never -1).
"""

import pandas as pd
import pandas_ta as ta


def klines_to_df(raw: list) -> pd.DataFrame:
    """Convert Binance raw klines list to a typed DataFrame."""
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all required indicators to the DataFrame."""
    df = df.copy()

    # Trend EMAs
    df["ema20"]  = ta.ema(df["close"], length=20)
    df["ema50"]  = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)

    # Momentum
    df["rsi"] = ta.rsi(df["close"], length=14)

    # Volatility
    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["atr"] = atr

    # Trend strength
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    df["adx"] = adx_df[f"ADX_14"] if adx_df is not None else None

    return df.dropna()


def get_signal(df: pd.DataFrame) -> dict:
    """
    Evaluate entry signal on the last CLOSED candle (index -2).
    Returns a dict with signal details.
    """
    if len(df) < 3:
        return {"signal": False, "reason": "insufficient data"}

    # Use the confirmed closed candle (never the live forming one)
    c  = df.iloc[-2]   # last closed candle
    c1 = df.iloc[-3]   # candle before that (for RSI crossover)

    checks = {}

    # 1 — Trend stack: EMA20 > EMA50 > EMA200
    checks["trend_aligned"] = (c["ema20"] > c["ema50"]) and (c["ema50"] > c["ema200"])

    # 2 — Regime: ADX > 20 (not a flat chop)
    checks["adx_ok"] = c["adx"] > 20 if c["adx"] is not None else False

    # 3 — RSI dipped into 42–55 on previous candle
    checks["rsi_dipped"] = 42 <= c1["rsi"] <= 55

    # 4 — RSI has now crossed back above 50 (momentum recovery)
    checks["rsi_cross"] = c1["rsi"] < 50 and c["rsi"] >= 50

    # 5 — Price above EMA50 (not in deep pullback)
    checks["price_above_ema50"] = c["close"] > c["ema50"]

    signal = all(checks.values())

    return {
        "signal": signal,
        "checks": checks,
        "close":  c["close"],
        "atr":    c["atr"],
        "ema20":  c["ema20"],
        "ema50":  c["ema50"],
        "ema200": c["ema200"],
        "rsi":    c["rsi"],
        "adx":    c["adx"],
    }
