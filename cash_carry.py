import logging
import time

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

log = logging.getLogger(__name__)

CASH_DEPLOY_THRESHOLD = 0.20  # deploy idle cash once it exceeds this fraction of equity
CASH_BUFFER = 0.10            # always keep at least this fraction of equity in cash
CARRY_SYMBOL = 'BIL'          # SPDR 0-3 Month T-Bill ETF -- see scope note below
MIN_ORDER_NOTIONAL = 100.00   # don't churn dust orders on tiny buffer drift

# SCOPE NOTE: the original recommendation this is based on also proposed
# FX-carry ETFs (UUP/FXY/FXB) and covered-call writing. Deliberately left
# out here: buying UUP/FXY/FXB outright (without shorting the funding
# currency on the other side) isn't a hedged carry trade, it's an
# un-backtested directional FX bet -- inconsistent with "low risk" and with
# every other strategy in this book, all of which have a validated edge
# before deployment. Covered calls need options infrastructure that doesn't
# exist yet. BIL is a genuine cash-equivalent (near-zero duration/credit
# risk), so it's the only sleeve implemented.

# INCIDENT NOTE (2026-07-04/05): the daily cron fired on Saturday and Sunday
# while the market was closed. Each run queued a market DAY buy sized off
# account.cash -- which only decrements on FILL, so the Saturday order being
# still unfilled made Sunday's run see the same "idle" cash and queue a
# second full-size buy. Both filled Monday at the open: $164.5K of BIL on
# $100K equity, cash -$57K. Hence the three guards below: market must be
# open, no pending CARRY_SYMBOL order may exist, and cash below the buffer
# now sells BIL back instead of silently staying leveraged.


def rebalance_idle_cash(trading_client) -> dict:
    """Keeps idle cash near CASH_BUFFER of equity using BIL as the parking
    vehicle: deploys excess once cash exceeds CASH_DEPLOY_THRESHOLD, and sells
    BIL back when cash has fallen below the buffer (e.g. strategies drew it
    down, or a past over-deployment left the account leveraged). Uses notional
    (dollar-denominated) market orders so no price lookup is needed."""
    try:
        clock = trading_client.get_clock()
    except Exception as e:
        log.error('Cash-carry: clock fetch failed: %s', e)
        return {'action': 'none', 'reason': f'clock unavailable: {e}'}
    if not clock.is_open:
        # A market order queued while closed sits unfilled, and account.cash
        # doesn't reflect it -- the next run would double-deploy.
        return {'action': 'none', 'reason': 'market closed'}

    account = trading_client.get_account()
    equity = float(account.equity)
    cash = float(account.cash)

    if equity <= 0:
        return {'action': 'none', 'reason': 'no equity'}

    try:
        pending = trading_client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[CARRY_SYMBOL])
        )
    except Exception as e:
        log.error('Cash-carry: open-order check failed: %s', e)
        return {'action': 'none', 'reason': f'open-order check failed: {e}'}
    if pending:
        return {'action': 'none', 'reason': f'{len(pending)} pending {CARRY_SYMBOL} order(s), cash not yet settled'}

    buffer = equity * CASH_BUFFER

    if cash < buffer:
        return _sell_back(trading_client, shortfall=round(buffer - cash, 2))

    cash_pct = cash / equity
    if cash_pct <= CASH_DEPLOY_THRESHOLD:
        return {'action': 'none', 'reason': f'cash at {cash_pct:.1%}, within {CASH_DEPLOY_THRESHOLD:.0%} threshold'}

    deployable = round(cash - buffer, 2)
    if deployable < MIN_ORDER_NOTIONAL:
        return {'action': 'none', 'reason': 'no deployable cash after buffer'}

    try:
        req = MarketOrderRequest(
            symbol=CARRY_SYMBOL,
            notional=deployable,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            client_order_id=f'CASHCARRY-{CARRY_SYMBOL}-{int(time.time())}',
        )
        order = trading_client.submit_order(req)
        log.info('Cash-carry: deployed $%.2f into %s (order %s)', deployable, CARRY_SYMBOL, order.id)
        return {'action': 'bought', 'symbol': CARRY_SYMBOL, 'notional': deployable, 'order_id': str(order.id)}
    except Exception as e:
        log.error('Cash-carry order failed: %s', e)
        return {'action': 'failed', 'error': str(e)}


def _sell_back(trading_client, shortfall: float) -> dict:
    """Sells enough BIL to bring cash back up to the buffer, capped at the
    whole position (a full liquidation uses close_position, since a notional
    sell for ~100% of a position can be rejected on price drift)."""
    if shortfall < MIN_ORDER_NOTIONAL:
        return {'action': 'none', 'reason': f'cash below buffer by ${shortfall:.2f}, under min order size'}

    try:
        position = trading_client.get_open_position(CARRY_SYMBOL)
        held = float(position.market_value)
    except Exception:
        held = 0.0
    if held <= 0:
        return {'action': 'none', 'reason': f'cash below buffer but no {CARRY_SYMBOL} held to sell'}

    try:
        if shortfall >= held:
            trading_client.close_position(CARRY_SYMBOL)
            log.warning('Cash-carry: liquidated entire %s position ($%.2f) to restore cash buffer', CARRY_SYMBOL, held)
            return {'action': 'sold_all', 'symbol': CARRY_SYMBOL, 'notional': held}
        req = MarketOrderRequest(
            symbol=CARRY_SYMBOL,
            notional=shortfall,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=f'CASHCARRY-{CARRY_SYMBOL}-{int(time.time())}',
        )
        order = trading_client.submit_order(req)
        log.info('Cash-carry: sold $%.2f of %s to restore cash buffer (order %s)', shortfall, CARRY_SYMBOL, order.id)
        return {'action': 'sold', 'symbol': CARRY_SYMBOL, 'notional': shortfall, 'order_id': str(order.id)}
    except Exception as e:
        log.error('Cash-carry sell-back failed: %s', e)
        return {'action': 'failed', 'error': str(e)}
