import os
import re
import time
import logging
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical.stock import StockHistoricalDataClient
from dotenv import load_dotenv

import risk_manager
import market_regime
import streak_analysis
import overnight_risk
import cash_carry

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

client = TradingClient(
    os.environ['ALPACA_API_KEY'],
    os.environ['ALPACA_SECRET'],
    paper=True
)

data_client = StockHistoricalDataClient(
    os.environ['ALPACA_API_KEY'],
    os.environ['ALPACA_SECRET'],
)

# Per-strategy base position size, as a fraction of equity. Reallocated
# 2026-07-03 away from the flat 5%-for-everyone baseline toward the sleeves
# with the strongest backtested edge (BTC/ETH/SOL trend following) and away
# from the ones with an unconfirmed or expiring edge (SMC has fired zero live
# signals; EMA Pullback is scheduled for EOL 2026-08-03). Total base exposure
# stays roughly flat (35% -> 36% summed) -- this is a reallocation, not new
# leverage. See vault: Sessions/DeepSeek Evaluation Follow-up + Risk Controls.
POSITION_PCT_BY_STRATEGY = {
    'SMC':      0.03,
    'EMAPB':    0.03,
    # XEMAX2 halved 5% -> 2.5% on 2026-07-14: live PF 0.73 / 25% WR over its
    # first 8 closed trades. Runs at half size until the 2026-08-03 alert
    # expiration, then renew-or-kill with the fuller sample.
    'XEMAX2':   0.025,
    'ORB':      0.04,
    'SOLTREND': 0.07,
    'BTCTREND': 0.08,
    'ETHTREND': 0.06,
}
DEFAULT_POSITION_PCT = 0.05  # untagged/legacy alerts, or any strategy code not listed above

# Crypto symbols: TradingView ticker → Alpaca symbol (slash format)
CRYPTO_MAP = {
    'BTCUSD':  'BTC/USD',
    'ETHUSD':  'ETH/USD',
    'SOLUSD':  'SOL/USD',
    'AVAXUSD': 'AVAX/USD',
    'XRPUSD':  'XRP/USD',
}

# Strategy code is an optional leading token (e.g. "SMC", "EMAPB", "XEMAX2") so
# alerts not yet updated with a strategy tag keep parsing exactly as before.
# Size is an optional trailing fraction (0-1) for strategies that compute their
# own dynamic position size (e.g. volatility-managed sizing); strategies that
# omit it keep their strategy's base_pct behavior unchanged.
_PATTERN = re.compile(
    r'(LONG|SHORT)\s*\|\s*(?:([A-Z0-9]{2,10})\s*\|\s*)?(\w+)\s*\|\s*Entry:\s*([\d.]+)\s*\|\s*SL:\s*([\d.]+)\s*\|\s*TP:\s*([\d.]+)'
    r'(?:\s*\|\s*Size:\s*([\d.]+))?'
)
_CLOSE_PATTERN = re.compile(r'CLOSE\s*\|\s*(?:([A-Z0-9]{2,10})\s*\|\s*)?(\w+)')


def parse_alert(msg: str):
    m = _PATTERN.search(msg.strip())
    if not m:
        return None
    return {
        'side':          m.group(1),
        'strategy':      m.group(2),  # None for untagged/legacy alerts
        'symbol':        m.group(3),
        'entry':         float(m.group(4)),
        'sl':            float(m.group(5)),
        'tp':            float(m.group(6)),
        'size_fraction': float(m.group(7)) if m.group(7) else None,
    }


def make_client_order_id(strategy: str, alpaca_symbol: str) -> str:
    tag = strategy or 'LEGACY'
    clean_symbol = alpaca_symbol.replace('/', '')
    return f'{tag}-{clean_symbol}-{int(time.time())}'


def position_symbol(alpaca_symbol: str) -> str:
    """Alpaca's position lookup/close endpoints want crypto symbols without the
    slash (e.g. 'SOLUSD'), while order submission wants the slash format
    ('SOL/USD'). Equities have no slash either way, so this is a no-op for them."""
    return alpaca_symbol.replace('/', '')


@app.route('/', methods=['GET'])
def index():
    return jsonify(status='ok', service='smc-webhook', endpoints=['/health', '/webhook'])


@app.route('/webhook', methods=['POST'])
def webhook():
    body = request.get_data(as_text=True)
    log.info('Alert received: %s', body)

    # Handle CLOSE signals (sent by Pine Script on exit via strategy.exit alert_message)
    close_match = _CLOSE_PATTERN.search(body.strip())
    if close_match:
        close_strategy = close_match.group(1)
        raw_symbol     = close_match.group(2)
        alpaca_symbol  = CRYPTO_MAP.get(raw_symbol, raw_symbol)
        lookup_symbol  = position_symbol(alpaca_symbol)
        try:
            client.get_open_position(lookup_symbol)
        except Exception:
            log.info('No open position for %s — already closed by bracket or never opened', alpaca_symbol)
            return jsonify(status='ok', action='already_closed', symbol=alpaca_symbol)
        try:
            client.close_position(lookup_symbol)
            log.info('Position closed: %s (strategy=%s)', alpaca_symbol, close_strategy or 'LEGACY')
            return jsonify(status='ok', action='closed', symbol=alpaca_symbol, strategy=close_strategy)
        except Exception as e:
            log.error('Close failed: %s', e)
            return jsonify(error=str(e)), 500

    # Any message without | delimiters is a raw order-fill name (e.g. "LX", "Long Exit") — ignore
    if '|' not in body.strip():
        log.info('Ignoring unstructured fill message: %s', body)
        return jsonify(status='ok', action='ignored'), 200

    signal = parse_alert(body)
    if not signal:
        log.warning('Could not parse alert: %s', body)
        return jsonify(error='Unrecognised alert format'), 400

    raw_symbol    = signal['symbol']
    alpaca_symbol = CRYPTO_MAP.get(raw_symbol, raw_symbol)
    is_crypto     = raw_symbol in CRYPTO_MAP

    # Alpaca doesn't support shorting crypto — a SHORT on a crypto symbol acts
    # only as a reversal exit: close any existing long, never open a short.
    # (Every XEMAX2 SHORT used to reach submit_order, get rejected for selling
    # coins the account didn't hold, and 500 — leaving TradingView's strategy
    # state out of sync with the live book. Jul 8/9/14 in the alert log.)
    if is_crypto and signal['side'] == 'SHORT':
        lookup_symbol = position_symbol(alpaca_symbol)
        try:
            client.get_open_position(lookup_symbol)
        except Exception:
            log.info('Crypto SHORT for %s with no long open — ignored (shorts unsupported)', alpaca_symbol)
            return jsonify(status='ok', action='ignored_crypto_short', symbol=alpaca_symbol)
        try:
            client.close_position(lookup_symbol)
            log.info('Crypto SHORT treated as reversal exit: closed %s long', alpaca_symbol)
            return jsonify(status='ok', action='closed_on_short_signal', symbol=alpaca_symbol)
        except Exception as e:
            log.error('Crypto reversal close failed for %s: %s', alpaca_symbol, e)
            return jsonify(error=str(e)), 500

    # Skip if already in a position for this symbol
    try:
        client.get_open_position(position_symbol(alpaca_symbol))
        log.info('Position already open for %s — skipping', alpaca_symbol)
        return jsonify(status='skipped', reason='position already open')
    except Exception:
        pass  # No existing position — proceed

    book_ok, book_reason = risk_manager.check_book_risk(client)
    if not book_ok:
        log.warning('Order blocked by book-level risk control: %s', book_reason)
        return jsonify(status='skipped', reason=book_reason)

    account = client.get_account()
    equity  = float(account.equity)
    side    = OrderSide.BUY if signal['side'] == 'LONG' else OrderSide.SELL

    base_pct = POSITION_PCT_BY_STRATEGY.get(signal['strategy'], DEFAULT_POSITION_PCT)

    # Strategies that send a Size fraction (e.g. volatility-managed sizing)
    # scale their base_pct by it; strategies that omit it are unaffected.
    effective_pct = base_pct
    if signal['size_fraction'] is not None:
        effective_pct = base_pct * max(0.0, min(signal['size_fraction'], 1.0))

    # Market-regime filter: reduce size when SPY's daily ADX shows a choppy
    # market. Applies only to flat-sized strategies — the vol-managed sleeves
    # (any alert carrying a Size fraction) already scale themselves to
    # realized volatility, and stacking a second volatility derating on top
    # would double-count the same signal (and SPY ADX is a poor chop proxy
    # for 24/7 crypto regardless). Fails open to 1.0 on data errors, so a
    # data hiccup never blocks trading outright.
    if signal['size_fraction'] is None:
        regime_multiplier = market_regime.get_regime_multiplier(data_client)
        effective_pct *= regime_multiplier
        if regime_multiplier < 1.0:
            log.info('Regime filter active: SPY ADX indicates chop, sizing at %.0f%%', regime_multiplier * 100)

    # Winning/losing streak multiplier — only kicks in once a strategy has a
    # full window of closed trades, so it can't chase noise on tiny samples.
    streak_multiplier = streak_analysis.get_streak_multiplier(client, signal['strategy'])
    effective_pct *= streak_multiplier
    if streak_multiplier != 1.0:
        log.info('Streak filter active for %s: sizing at %.0f%%', signal['strategy'], streak_multiplier * 100)

    if is_crypto:
        qty = round((equity * effective_pct) / signal['entry'], 6)
        tif = TimeInForce.GTC
    else:
        qty = max(1, int((equity * effective_pct) / signal['entry']))
        # GTC (was DAY): with DAY, bracket TP/SL legs expired at the close,
        # leaving overnight positions unprotected — an AAPL TP was hit after
        # its leg had expired and never filled. GTC keeps brackets working
        # until they actually execute.
        tif = TimeInForce.GTC

    if qty <= 0:
        log.info('Computed qty <= 0 for %s (size_fraction=%s) — skipping', alpaca_symbol, signal['size_fraction'])
        return jsonify(status='skipped', reason='qty <= 0 from size_fraction')

    exposure_ok, exposure_reason = risk_manager.check_underlying_exposure(
        client, alpaca_symbol, qty * signal['entry'], equity
    )
    if not exposure_ok:
        log.warning('Order blocked by per-underlying exposure cap: %s', exposure_reason)
        return jsonify(status='skipped', reason=exposure_reason)

    client_order_id = make_client_order_id(signal['strategy'], alpaca_symbol)

    log.info('Submitting %s %s %s  entry=%.4f  SL=%.4f  TP=%.4f  size_fraction=%s  client_order_id=%s',
             side.value.upper(), qty, alpaca_symbol,
             signal['entry'], signal['sl'], signal['tp'], signal['size_fraction'], client_order_id)

    try:
        if is_crypto:
            # Alpaca does not support bracket orders for crypto
            req = MarketOrderRequest(
                symbol          = alpaca_symbol,
                qty             = qty,
                side            = side,
                time_in_force   = tif,
                client_order_id = client_order_id,
            )
        else:
            req = MarketOrderRequest(
                symbol          = alpaca_symbol,
                qty             = qty,
                side            = side,
                time_in_force   = tif,
                order_class     = OrderClass.BRACKET,
                take_profit     = TakeProfitRequest(limit_price=round(signal['tp'], 2)),
                stop_loss       = StopLossRequest(stop_price=round(signal['sl'], 2)),
                client_order_id = client_order_id,
            )
        order = client.submit_order(req)
        log.info('Order submitted: %s (client_order_id=%s)', order.id, client_order_id)
        return jsonify(status='ok', order_id=str(order.id), client_order_id=client_order_id, qty=qty, symbol=alpaca_symbol)
    except Exception as e:
        log.error('Order failed: %s', e)
        return jsonify(error=str(e)), 500


@app.route('/health', methods=['GET'])
def health():
    acct = client.get_account()
    return jsonify(status='ok', equity=acct.equity, buying_power=acct.buying_power)


@app.route('/risk/gap-check', methods=['GET', 'POST'])
def gap_check():
    """Closes any open equity position whose move vs. yesterday's close
    exceeds 1.5x its ATR(14). No internal scheduler here — call this from an
    external cron (e.g. the same cron-job.org account pinging /health) once
    shortly after the 9:30 ET open."""
    actions = overnight_risk.check_positions(client, data_client)
    return jsonify(status='ok', checked_at=time.time(), actions=actions)


@app.route('/risk/cash-carry', methods=['GET', 'POST'])
def cash_carry_check():
    """Deploys idle cash above the buffer into BIL (T-bills) once it exceeds
    20% of equity. No internal scheduler — call from an external cron, e.g.
    once daily. See cash_carry.py for what was deliberately left out of scope
    (FX-carry ETFs, covered calls) and why."""
    result = cash_carry.rebalance_idle_cash(client)
    return jsonify(status='ok', checked_at=time.time(), result=result)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
