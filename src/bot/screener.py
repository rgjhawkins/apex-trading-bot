"""
Volume screener — ranks all Binance USDT pairs by 24h volume in a
single API call, then returns the top N as the pair list for the tick.

Cache TTL is 5 minutes so the list refreshes without hammering the API
on every tick.
"""

import time

# Leveraged / inverse tokens and stablecoin bases to skip
_EXCLUDE_BASES    = {"USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "UST", "USTC"}
_EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "3LUSDT", "3SUSDT")

_CACHE_TTL = 300.0   # 5 minutes


def get_top_pairs(
    client,
    top_n:      int   = 30,
    min_volume: float = 0.0,
    exclude:    list  = None,
    _cache:     dict  = {},
) -> tuple[list[str], list[dict]]:
    """
    Return (pair_list, ranked_info) where:
      - pair_list   = top `top_n` symbols passing filters, sorted by 24h volume
      - ranked_info = list of {symbol, volume_usdt, change_pct} for the UI

    One API call fetches all tickers; cached for 5 minutes.
    On error, returns previous cached result so the bot keeps running.
    """
    now = time.monotonic()
    if _cache.get("ranked") and (now - _cache.get("ts", 0)) < _CACHE_TTL:
        ranked = _cache["ranked"]
    else:
        try:
            tickers = client.get_ticker()          # one call — all ~400 pairs
        except Exception:
            ranked = _cache.get("ranked") or []

        else:
            exclude_set = set(exclude or [])
            rows = []
            for t in tickers:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                base = sym[:-4]
                if base in _EXCLUDE_BASES:
                    continue
                if any(sym.endswith(s) for s in _EXCLUDE_SUFFIXES):
                    continue
                if sym in exclude_set:
                    continue
                rows.append({
                    "symbol":      sym,
                    "volume_usdt": float(t.get("quoteVolume", 0)),
                    "change_pct":  float(t.get("priceChangePercent", 0)),
                })

            rows.sort(key=lambda x: x["volume_usdt"], reverse=True)
            ranked = rows
            _cache["ranked"] = ranked
            _cache["ts"]     = now

    # Apply live filters (don't need a re-fetch)
    filtered = [r for r in ranked if r["volume_usdt"] >= min_volume]
    top      = filtered[:top_n]
    return [r["symbol"] for r in top], top
