"""
Technical indicator calculations from raw Binance kline data.
Pure pandas/numpy — no external TA library required.
All signals are generated on CLOSED candles only (index -2, never -1).
"""

import pandas as pd
import numpy as np


def klines_to_df(raw: list) -> pd.DataFrame:
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


# ── Private indicator helpers ──────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
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
    di_plus    = 100 * dm_plus_s  / atr_s.replace(0, np.nan)
    di_minus   = 100 * dm_minus_s / atr_s.replace(0, np.nan)
    dx         = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    ema_fast    = series.ewm(span=fast,   adjust=False).mean()
    ema_slow    = series.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line   # histogram


def _volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    avg = volume.rolling(period).mean()
    return (volume / avg.replace(0, np.nan)).fillna(1.0)


def _body_pct(open_: pd.Series, close: pd.Series,
              high: pd.Series, low: pd.Series) -> pd.Series:
    body  = (close - open_).abs()
    range_ = (high - low).replace(0, np.nan)
    return (body / range_ * 100.0).fillna(0)


# ── Public API ─────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"]        = _ema(df["close"], 20)
    df["ema50"]        = _ema(df["close"], 50)
    df["ema200"]       = _ema(df["close"], 200)
    df["rsi"]          = _rsi(df["close"], 14)
    df["atr"]          = _atr(df["high"], df["low"], df["close"], 14)
    df["adx"]          = _adx(df["high"], df["low"], df["close"], 14)
    df["macd_hist"]    = _macd(df["close"], 12, 26, 9)
    df["volume_ratio"] = _volume_ratio(df["volume"], 20)
    df["body_pct"]     = _body_pct(df["open"], df["close"], df["high"], df["low"])
    return df.dropna()


def get_signal(df: pd.DataFrame, rules: dict = None) -> dict:
    """
    Evaluate entry signal on the last CLOSED candle (index -2).
    Never touches the live forming candle (index -1).
    New checks default to True when their _enabled flag is False,
    so they are transparent to the base strategy when disabled.
    """
    rules = rules or {}

    rsi_low   = rules.get("rsi_dip_low",    42.0)
    rsi_high  = rules.get("rsi_dip_high",   55.0)
    rsi_cross = rules.get("rsi_cross_level", 50.0)
    adx_min   = rules.get("adx_min",        20.0)

    if len(df) < 3:
        return {"signal": False, "reason": "insufficient data"}

    c  = df.iloc[-2]   # last closed candle
    c1 = df.iloc[-3]   # previous candle (for RSI crossover)

    # ── Core checks (EMA trend stack always active) ────────────────
    checks = {
        "trend_aligned":     bool(c["ema20"] > c["ema50"] > c["ema200"]),
        "price_above_ema50": bool(c["close"] > c["ema50"]),
    }

    if rules.get("rsi_enabled", True):
        checks["rsi_dipped"] = bool(rsi_low <= c1["rsi"] <= rsi_high)
        checks["rsi_cross"]  = bool(c1["rsi"] < rsi_cross and c["rsi"] >= rsi_cross)

    if rules.get("adx_enabled", True):
        checks["adx_ok"] = bool(c["adx"] > adx_min)

    # ── Optional entry filters ─────────────────────────────────────
    if rules.get("volume_spike_enabled", False):
        mult = rules.get("volume_spike_mult", 1.5)
        checks["volume_spike"] = bool(c["volume_ratio"] >= mult)

    if rules.get("macd_filter_enabled", False):
        mode = rules.get("macd_mode", "positive")
        if mode == "turning_up":
            checks["macd_ok"] = bool(c["macd_hist"] > c1["macd_hist"])
        else:
            checks["macd_ok"] = bool(c["macd_hist"] > 0)

    if rules.get("body_filter_enabled", False):
        min_body = rules.get("body_filter_pct", 50.0)
        checks["body_ok"] = bool(c["body_pct"] >= min_body)

    return {
        "signal":       all(checks.values()),
        "checks":       checks,
        "close":        float(c["close"]),
        "atr":          float(c["atr"]),
        "ema20":        float(c["ema20"]),
        "ema50":        float(c["ema50"]),
        "ema200":       float(c["ema200"]),
        "rsi":          float(c["rsi"]),
        "adx":          float(c["adx"]),
        "macd_hist":    float(c["macd_hist"]),
        "volume_ratio": float(c["volume_ratio"]),
    }


def get_daytrading_signal(df: pd.DataFrame, rules: dict = None) -> dict:
    """
    Day-trading entry: price rose X% over the last N closed candles + volume confirmation.
    Evaluated on the last CLOSED candle (index -2).
    """
    rules    = rules or {}
    lookback = max(1, int(rules.get("dt_lookback_candles", 3)))

    if len(df) < lookback + 2:
        return {"signal": False, "checks": {}}

    c    = df.iloc[-2]            # last closed candle
    prev = df.iloc[-2 - lookback] # N candles before

    curr_close     = float(c["close"])
    prev_close     = float(prev["close"])
    price_rise_pct = (curr_close - prev_close) / prev_close * 100

    checks = {
        "price_breakout": price_rise_pct >= rules.get("dt_price_rise_pct", 1.5),
        "volume_confirm": float(c["volume_ratio"]) >= rules.get("dt_volume_mult", 2.0),
        "above_ema20":    curr_close > float(c["ema20"]),
        "rsi_not_overbought": float(c["rsi"]) <= rules.get("dt_max_rsi", 72.0),
    }

    return {
        "signal":         all(checks.values()),
        "checks":         checks,
        "close":          curr_close,
        "atr":            float(c["atr"]),
        "price_rise_pct": price_rise_pct,
        "volume_ratio":   float(c["volume_ratio"]),
    }
