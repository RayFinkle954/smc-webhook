import logging
import time

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

log = logging.getLogger(__name__)

REGIME_SYMBOL = "SPY"
ADX_PERIOD = 14
ADX_LOOKBACK_DAYS = 90  # comfortably more than needed for Wilder smoothing to stabilize
CACHE_TTL_SECONDS = 3600  # ADX is a daily-bar indicator; no need to refetch more than hourly

_cache = {"adx": None, "fetched_at": 0}


def _wilder_adx(highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
    """Wilder's ADX. Needs at least ~2*period+1 bars to produce a stable value."""
    n = len(closes)
    if n < period * 2 + 1:
        return None

    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    def wilder_smooth(values: list[float], period: int) -> list[float]:
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    smoothed_tr = wilder_smooth(tr, period)
    smoothed_plus_dm = wilder_smooth(plus_dm, period)
    smoothed_minus_dm = wilder_smooth(minus_dm, period)

    dx_values = []
    for str_, spdm, smdm in zip(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm):
        if str_ == 0:
            continue
        plus_di = 100 * spdm / str_
        minus_di = 100 * smdm / str_
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0
        dx_values.append(dx)

    if len(dx_values) < period:
        return None

    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _fetch_adx(data_client: StockHistoricalDataClient) -> float | None:
    request = StockBarsRequest(
        symbol_or_symbols=REGIME_SYMBOL,
        timeframe=TimeFrame.Day,
        limit=ADX_LOOKBACK_DAYS,
    )
    barset = data_client.get_stock_bars(request)
    bars = barset.data.get(REGIME_SYMBOL, [])
    if len(bars) < ADX_PERIOD * 2 + 1:
        log.warning("Not enough %s daily bars for ADX (%d)", REGIME_SYMBOL, len(bars))
        return None
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    return _wilder_adx(highs, lows, closes, ADX_PERIOD)


def get_regime_multiplier(data_client: StockHistoricalDataClient) -> float:
    """Scales position size down in choppy markets (low SPY ADX). Cached
    hourly since this is a daily-bar indicator — no need to hit the market
    data API on every webhook call."""
    now = time.time()
    if _cache["adx"] is None or now - _cache["fetched_at"] > CACHE_TTL_SECONDS:
        try:
            adx = _fetch_adx(data_client)
        except Exception as e:
            log.error("ADX fetch failed, defaulting to full size: %s", e)
            adx = None
        _cache["adx"] = adx
        _cache["fetched_at"] = now

    adx = _cache["adx"]
    if adx is None:
        return 1.0  # fail open — don't block trading over a data hiccup
    if adx < 20:
        return 0.5
    if adx < 30:
        return 0.8
    return 1.0
