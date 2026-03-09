"""
Technical indicator calculations from raw Binance kline data.
Pure pandas/numpy — no external TA library required.
All signals are generated on CLOSED candles only (index -2, never -1).
"""

import pandas as pd
import numpy as np


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


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    # Wilder smoothing (equivalent to EMA with alpha=1/period)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    # Wilder smoothing
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    dm_plus  = np.where((high - prev_high) > (prev_low - low),
                        (high - prev_high).clip(lower=0), 0)
    dm_minus = np.where((prev_low - low) > (high - prev_high),
                        (prev_low - low).clip(lower=0), 0)

    dm_plus_s  = pd.Series(dm_plus,  index=high.index).ewm(alpha=1 / period, adjust=False).mean()
    dm_minus_s = pd.Series(dm_minus, index=high.index).ewm(alpha=1 / period, adjust=False).mean()
    atr_s      = tr.ewm(alpha=1 / period, adjust=False).mean()

    di_plus  = 100 * dm_plus_s  / atr_s.replace(0, np.nan)
    di_minus = 100 * dm_minus_s / atr_s.replace(0, np.nan)
    dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx      = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx.fillna(0)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"]  = _ema(df["close"], 20)
    df["ema50"]  = _ema(df["close"], 50)
    df["ema200"] = _ema(df["close"], 200)
    df["rsi"]    = _rsi(df["close"], 14)
    df["atr"]    = _atr(df["high"], df["low"], df["close"], 14)
    df["adx"]    = _adx(df["high"], df["low"], df["close"], 14)
    return df.dropna()


def get_signal(df: pd.DataFrame) -> dict:
    """
    Evaluate entry signal on the last CLOSED candle (index -2).
    Never uses the live forming candle (index -1).
    """
    if len(df) < 3:
        return {"signal": False, "reason": "insufficient data"}

    c  = df.iloc[-2]   # last closed candle
    c1 = df.iloc[-3]   # candle before that (for RSI crossover)

    checks = {
        "trend_aligned":    bool(c["ema20"] > c["ema50"] > c["ema200"]),
        "adx_ok":           bool(c["adx"] > 20),
        "rsi_dipped":       bool(42 <= c1["rsi"] <= 55),
        "rsi_cross":        bool(c1["rsi"] < 50 and c["rsi"] >= 50),
        "price_above_ema50": bool(c["close"] > c["ema50"]),
    }

    return {
        "signal": all(checks.values()),
        "checks": checks,
        "close":  float(c["close"]),
        "atr":    float(c["atr"]),
        "ema20":  float(c["ema20"]),
        "ema50":  float(c["ema50"]),
        "ema200": float(c["ema200"]),
        "rsi":    float(c["rsi"]),
        "adx":    float(c["adx"]),
    }
