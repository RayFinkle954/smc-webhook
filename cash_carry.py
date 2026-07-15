import logging
import time

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

log = logging.getLogger(__name__)

CASH_DEPLOY_THRESHOLD = 0.20  # deploy idle cash once it exceeds this fraction of equity
CASH_BUFFER = 0.10            # always keep at least this fraction of equity in cash
CARRY_SYMBOL = 'BIL'          # SPDR 0-3 Month T-Bill ETF -- see scope note below
MIN_ORDER_NOTIONAL = 100.00   # don't churn dust orders on tiny buffer drift
CARRY_ALARM_MULT = 1.20       # BIL above this fraction of equity = runaway sweep, scream

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

# INCIDENT NOTE (2026-07-14): Alpaca crypto buys require SETTLED cash --
# same-day BIL-sale proceeds are T+1 and don't count (the ETHTREND replay had
# to be resized to 0.95x to fit). Worse, the weekly trend sleeves signal
# Friday 20:00 ET, when the equity market is closed and BIL can't be sold at
# all. A sell-back can therefore never fund a same-day crypto entry: the
# settled cash has to already be there. Fix: the buffer below is dynamically
# widened to reserve settled cash for every crypto sleeve that is currently
# FLAT (a flat sleeve may fire a fresh entry at any time; a sleeve already in
# a position won't need entry cash). The reserve shrinks back to the plain
# CASH_BUFFER as crypto positions fill, so the drag only exists while entries
# are actually possible. Because the reserve is maintained on every weekday
# run, it is settled by the time a Friday-evening signal needs it.
#
# COST, quantified and accepted 2026-07-15: worst case (all four crypto
# sleeves flat at once) the buffer holds ~29.6% of equity as raw settled cash
# instead of BIL -- at ~4.5% T-bill yield that's ~1.3% of equity per year of
# forgone carry, versus a missed/failed weekly trend entry, which the book is
# built around. The drag self-heals as sleeves deploy.

# Crypto-trading sleeves and the (no-slash) Alpaca position symbol whose
# presence means AT LEAST ONE sleeve mapped to it is deployed. Positions are
# not attributed per strategy, so sleeves sharing a symbol (BTCTREND and
# XEMAX2 both trade BTCUSD) are handled as a group in
# crypto_cash_reserve_pct: when the shared symbol is held we still reserve
# for the group's worst case -- every sleeve flat except the smallest.
CRYPTO_SLEEVE_SYMBOLS = {
    'BTCTREND': 'BTCUSD',
    'ETHTREND': 'ETHUSD',
    'SOLTREND': 'SOLUSD',
    'XEMAX2':   'BTCUSD',
}
# A fresh entry can be upsized by the streak multiplier, so the reserve must
# cover the LARGEST size the webhook can actually request, not the base size.
# Regression-tested against streak_analysis.MULT_HOT (the regime multiplier
# only ever derates, and size_fraction is capped at 1.0, so streak is the
# only upsizer).
MAX_ENTRY_UPSIZE = 1.2
# On top of that: price drift between the signal price and the market fill.
CRYPTO_RESERVE_SAFETY = 1.05
# Deliberately small hysteresis above a widened buffer -- just enough that
# daily equity drift doesn't churn tiny BIL orders. (NOT the original
# 10-point buffer->deploy gap: that much headroom above the reserve would
# idle up to 10 more points of equity out of BIL for no benefit.)
DEPLOY_HYSTERESIS = 0.02


def crypto_cash_reserve_pct(trading_client, position_pct_by_strategy):
    """Fraction of equity to keep as settled cash for potential fresh crypto
    entries, sized to the largest order the webhook can actually submit
    (base size x max streak upsize x fill-drift margin). Sleeves are grouped
    by position symbol: a held symbol proves only that SOME sleeve in the
    group is deployed, so the group still reserves for its worst case --
    everything flat except the smallest sleeve.

    Returns None when positions can't be fetched: the caller must skip the
    rebalance entirely rather than trade on unknown state (a reserve guessed
    from a failed API call once meant a real BIL sell order on a blip)."""
    if not position_pct_by_strategy:
        return 0.0
    try:
        held = {p.symbol.replace('/', '') for p in trading_client.get_all_positions()}
    except Exception as e:
        log.error('Cash-carry: position fetch failed — skipping this rebalance: %s', e)
        return None

    needs_by_symbol: dict = {}
    for code, symbol in CRYPTO_SLEEVE_SYMBOLS.items():
        pct = position_pct_by_strategy.get(code)
        if pct is not None:
            need = pct * MAX_ENTRY_UPSIZE * CRYPTO_RESERVE_SAFETY
            needs_by_symbol.setdefault(symbol, []).append(need)

    reserve = 0.0
    for symbol, needs in needs_by_symbol.items():
        if symbol in held:
            reserve += sum(needs) - min(needs)
        else:
            reserve += sum(needs)
    return reserve


def rebalance_idle_cash(trading_client, position_pct_by_strategy=None) -> dict:
    """Keeps idle cash near the effective buffer using BIL as the parking
    vehicle: deploys excess once cash exceeds the deploy threshold, and sells
    BIL back when cash has fallen below the buffer (e.g. strategies drew it
    down, or a past over-deployment left the account leveraged). Uses notional
    (dollar-denominated) market orders so no price lookup is needed.

    When the caller passes its per-strategy sizing dict, the buffer widens to
    max(CASH_BUFFER, crypto reserve) so flat crypto sleeves always have
    settled cash waiting -- see the 2026-07-14 incident note above."""
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

    # Runaway-sweep alarm: the 7/4-7/5 incident put $164.5K of BIL on $100K
    # equity and nothing noticed for 8 days. The guards above should make a
    # repeat impossible, but if BIL ever exceeds CARRY_ALARM_MULT x equity
    # again, log at ERROR (visible in Render logs) and flag it in the cron's
    # JSON response so it can't sit silent.
    alarm = None
    try:
        carry_value = float(trading_client.get_open_position(CARRY_SYMBOL).market_value)
    except Exception:
        carry_value = 0.0
    if carry_value > equity * CARRY_ALARM_MULT:
        alarm = f'{CARRY_SYMBOL} at ${carry_value:,.0f} = {carry_value / equity:.0%} of equity (limit {CARRY_ALARM_MULT:.0%})'
        log.error('CASH-CARRY ALARM: %s — runaway sweep, investigate immediately', alarm)

    reserve_pct = crypto_cash_reserve_pct(trading_client, position_pct_by_strategy)
    if reserve_pct is None:
        result = {'action': 'none', 'reason': 'position fetch failed; skipping rebalance rather than trading on unknown state'}
        if alarm:
            result['alarm'] = alarm
        return result
    buffer_pct = max(CASH_BUFFER, reserve_pct)
    deploy_threshold = max(CASH_DEPLOY_THRESHOLD, buffer_pct + DEPLOY_HYSTERESIS)
    buffer = equity * buffer_pct

    def annotate(result: dict) -> dict:
        if alarm:
            result['alarm'] = alarm
        result['crypto_reserve_pct'] = round(reserve_pct, 4)
        result['buffer_pct'] = round(buffer_pct, 4)
        return result

    if cash < buffer:
        return annotate(_sell_back(trading_client, shortfall=round(buffer - cash, 2)))

    cash_pct = cash / equity
    if cash_pct <= deploy_threshold:
        return annotate({'action': 'none', 'reason': f'cash at {cash_pct:.1%}, within {deploy_threshold:.0%} threshold'})

    deployable = round(cash - buffer, 2)
    if deployable < MIN_ORDER_NOTIONAL:
        return annotate({'action': 'none', 'reason': 'no deployable cash after buffer'})

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
        return annotate({'action': 'bought', 'symbol': CARRY_SYMBOL, 'notional': deployable, 'order_id': str(order.id)})
    except Exception as e:
        log.error('Cash-carry order failed: %s', e)
        return annotate({'action': 'failed', 'error': str(e)})


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
