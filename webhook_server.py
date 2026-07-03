import os
import re
import time
import logging
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from dotenv import load_dotenv

import risk_manager

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

client = TradingClient(
    os.environ['ALPACA_API_KEY'],
    os.environ['ALPACA_SECRET'],
    paper=True
)

POSITION_PCT = 0.05

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
# omit it keep the flat POSITION_PCT behavior unchanged.
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

    # Strategies that send a Size fraction (e.g. volatility-managed sizing)
    # scale the flat POSITION_PCT by it; strategies that omit it are unaffected.
    effective_pct = POSITION_PCT
    if signal['size_fraction'] is not None:
        effective_pct = POSITION_PCT * max(0.0, min(signal['size_fraction'], 1.0))

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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
