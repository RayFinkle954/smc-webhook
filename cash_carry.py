import logging
import time

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

log = logging.getLogger(__name__)

CASH_DEPLOY_THRESHOLD = 0.20  # deploy idle cash once it exceeds this fraction of equity
CASH_BUFFER = 0.10            # always keep at least this fraction of equity in cash
CARRY_SYMBOL = 'BIL'          # SPDR 0-3 Month T-Bill ETF -- see scope note below

# SCOPE NOTE: the original recommendation this is based on also proposed
# FX-carry ETFs (UUP/FXY/FXB) and covered-call writing. Deliberately left
# out here: buying UUP/FXY/FXB outright (without shorting the funding
# currency on the other side) isn't a hedged carry trade, it's an
# un-backtested directional FX bet -- inconsistent with "low risk" and with
# every other strategy in this book, all of which have a validated edge
# before deployment. Covered calls need options infrastructure that doesn't
# exist yet. BIL is a genuine cash-equivalent (near-zero duration/credit
# risk), so it's the only sleeve implemented.


def rebalance_idle_cash(trading_client) -> dict:
    """Deploys excess idle cash into BIL once cash exceeds CASH_DEPLOY_THRESHOLD
    of equity, keeping CASH_BUFFER in reserve for margin/order needs. Uses a
    notional (dollar-denominated) market order so no price lookup is needed."""
    account = trading_client.get_account()
    equity = float(account.equity)
    cash = float(account.cash)

    if equity <= 0:
        return {'action': 'none', 'reason': 'no equity'}

    cash_pct = cash / equity
    if cash_pct <= CASH_DEPLOY_THRESHOLD:
        return {'action': 'none', 'reason': f'cash at {cash_pct:.1%}, within {CASH_DEPLOY_THRESHOLD:.0%} threshold'}

    buffer = equity * CASH_BUFFER
    deployable = round(cash - buffer, 2)
    if deployable <= 0:
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
