import logging

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import AssetClass, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from cash_carry import CARRY_SYMBOL

log = logging.getLogger(__name__)

ATR_PERIOD = 14
ATR_LOOKBACK_DAYS = 30
GAP_ATR_MULTIPLIER = 1.5

# NOTE: this checks every open equity position's move vs. its previous close,
# not specifically whether the position was opened before today — Alpaca's
# Position model doesn't expose an "opened before today" flag, and a large
# adverse gap is worth reacting to whether the position is from yesterday or
# this morning's open. Triggered by an external cron hitting /risk/gap-check
# (same cron-job.org pattern as the existing /health keep-alive) since this
# server has no internal scheduler — meant to run once shortly after the
# 9:30 ET open.


def _wilder_atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
    n = len(closes)
    if n < period + 1:
        return None
    tr = []
    for i in range(1, n):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    atr = sum(tr[:period]) / period
    for t in tr[period:]:
        atr = (atr * (period - 1) + t) / period
    return atr


def _get_atr(data_client, symbol: str) -> float | None:
    try:
        request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, limit=ATR_LOOKBACK_DAYS)
        bars = data_client.get_stock_bars(request).data.get(symbol, [])
        if len(bars) < ATR_PERIOD + 1:
            return None
        return _wilder_atr([b.high for b in bars], [b.low for b in bars], [b.close for b in bars], ATR_PERIOD)
    except Exception as e:
        log.error('ATR fetch failed for %s: %s', symbol, e)
        return None


def check_positions(trading_client, data_client) -> list[dict]:
    """Closes any open equity position whose move from yesterday's close
    exceeds GAP_ATR_MULTIPLIER x its ATR(14). Crypto is skipped — it trades
    24/7 so "gap" isn't a meaningful concept the way it is for equities with
    a closed overnight session. Returns a list of actions taken, for logging."""
    actions = []
    try:
        positions = trading_client.get_all_positions()
    except Exception as e:
        log.error('Could not fetch positions for gap check: %s', e)
        return actions

    for position in positions:
        if position.asset_class == AssetClass.CRYPTO:
            continue
        if position.symbol == CARRY_SYMBOL:
            # BIL drops by its distribution every ex-dividend date — with a
            # near-zero ATR that reads as a "gap" and would dump the whole
            # T-bill sleeve over a payout, not a risk event.
            continue
        if not position.current_price or not position.lastday_price:
            continue

        current = float(position.current_price)
        lastday = float(position.lastday_price)
        if lastday == 0:
            continue
        gap = abs(current - lastday)

        atr = _get_atr(data_client, position.symbol)
        if atr is None or atr == 0:
            continue

        if gap > atr * GAP_ATR_MULTIPLIER:
            try:
                # Equity positions here were opened as bracket orders, so the
                # position's qty is held by the open TP/SL legs — Alpaca
                # rejects a close on held qty. Cancel this symbol's open
                # orders first, then close.
                open_orders = trading_client.get_orders(
                    GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[position.symbol])
                )
                for order in open_orders:
                    trading_client.cancel_order_by_id(order.id)
                trading_client.close_position(position.symbol)
                log.warning('Gap-risk exit: %s moved %.2f vs ATR %.2f (%.1fx) — closed',
                            position.symbol, gap, atr, gap / atr)
                actions.append({'symbol': position.symbol, 'gap': gap, 'atr': atr, 'action': 'closed'})
            except Exception as e:
                log.error('Gap-risk close failed for %s: %s', position.symbol, e)
                actions.append({'symbol': position.symbol, 'gap': gap, 'atr': atr, 'action': 'close_failed', 'error': str(e)})

    return actions
