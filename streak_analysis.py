import logging
import time

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 900  # trades are infrequent; no need to re-pull order history every webhook call
STREAK_WINDOW = 5

_cache: dict = {}  # strategy_code -> {"multiplier": float, "fetched_at": ts}


def _num(v):
    return float(v) if v not in (None, '') else None


def _build_symbol_timeline(orders) -> dict:
    """Same fill-pairing approach as the dashboard's kpis.js buildTrades: each
    order's own fill is an entry candidate, and each filled bracket leg is an
    exit candidate (legs aren't returned as separate top-level orders)."""
    by_symbol: dict = {}
    for o in orders:
        if o.filled_avg_price and o.filled_at:
            by_symbol.setdefault(o.symbol, []).append({
                'time': o.filled_at,
                'side': o.side.value if o.side else None,
                'qty': _num(o.filled_qty or o.qty),
                'price': _num(o.filled_avg_price),
            })
        for leg in (o.legs or []):
            if leg.status == 'filled' and leg.filled_avg_price:
                opposite = 'sell' if (o.side and o.side.value == 'buy') else 'buy'
                by_symbol.setdefault(o.symbol, []).append({
                    'time': leg.filled_at,
                    'side': opposite,
                    'qty': _num(leg.filled_qty or leg.qty or o.qty),
                    'price': _num(leg.filled_avg_price),
                })
    for fills in by_symbol.values():
        fills.sort(key=lambda f: f['time'])
    return by_symbol


def _closed_trade_pnls_newest_first(orders) -> list[float]:
    timelines = _build_symbol_timeline(orders)
    pnls = []  # (entry_time, pnl_dollars)
    for fills in timelines.values():
        open_entry = None
        for fill in fills:
            direction = 1 if fill['side'] == 'buy' else -1
            if open_entry is None:
                open_entry = {'price': fill['price'], 'qty': fill['qty'], 'direction': direction, 'time': fill['time']}
                continue
            if direction == open_entry['direction']:
                continue  # same-side fill while open — not modeled, ignore (mirrors kpis.js)
            pnl = (fill['price'] - open_entry['price']) * open_entry['qty'] * open_entry['direction']
            pnls.append((open_entry['time'], pnl))
            open_entry = None
    pnls.sort(key=lambda p: p[0], reverse=True)
    return [pnl for _, pnl in pnls]


def get_streak_multiplier(client, strategy_code: str | None, window: int = STREAK_WINDOW) -> float:
    """Scales position size by recent win rate for this strategy: >60% over
    the last `window` closed trades -> 1.2x, <40% -> 0.8x, otherwise 1.0x.
    Requires a full window of closed trades before adjusting anything, so a
    strategy with only a handful of live trades (most of them, right now)
    just gets 1.0x rather than a multiplier chasing noise."""
    if not strategy_code:
        return 1.0

    now = time.time()
    cached = _cache.get(strategy_code)
    if cached and now - cached['fetched_at'] < CACHE_TTL_SECONDS:
        return cached['multiplier']

    try:
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, nested=True, limit=200)
        orders = client.get_orders(req)
        orders = [o for o in orders if o.client_order_id and o.client_order_id.startswith(f'{strategy_code}-')]
        pnls = _closed_trade_pnls_newest_first(orders)[:window]
    except Exception as e:
        log.error('Streak lookup failed for %s, defaulting to 1.0x: %s', strategy_code, e)
        return 1.0  # fail open — don't cache a failure, retry next call

    if len(pnls) < window:
        multiplier = 1.0
    else:
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        if win_rate > 0.6:
            multiplier = 1.2
        elif win_rate < 0.4:
            multiplier = 0.8
        else:
            multiplier = 1.0

    _cache[strategy_code] = {'multiplier': multiplier, 'fetched_at': now}
    return multiplier
