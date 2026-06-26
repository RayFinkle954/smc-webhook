import os
import re
import logging
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

client = TradingClient(
    os.environ['ALPACA_API_KEY'],
    os.environ['ALPACA_SECRET'],
    paper=True
)

POSITION_PCT = 0.10

# Crypto symbols: TradingView ticker → Alpaca symbol (slash format)
CRYPTO_MAP = {
    'BTCUSD':  'BTC/USD',
    'ETHUSD':  'ETH/USD',
    'SOLUSD':  'SOL/USD',
    'AVAXUSD': 'AVAX/USD',
    'XRPUSD':  'XRP/USD',
}

_PATTERN = re.compile(
    r'(LONG|SHORT) \| (\w+) \| Entry: ([\d.]+) \| SL: ([\d.]+) \| TP: ([\d.]+)'
)


def parse_alert(msg: str):
    m = _PATTERN.search(msg.strip())
    if not m:
        return None
    return {
        'side':   m.group(1),
        'symbol': m.group(2),
        'entry':  float(m.group(3)),
        'sl':     float(m.group(4)),
        'tp':     float(m.group(5)),
    }


@app.route('/', methods=['GET'])
def index():
    return jsonify(status='ok', service='smc-webhook', endpoints=['/health', '/webhook'])


@app.route('/webhook', methods=['POST'])
def webhook():
    body = request.get_data(as_text=True)
    log.info('Alert received: %s', body)

    signal = parse_alert(body)
    if not signal:
        log.warning('Could not parse alert: %s', body)
        return jsonify(error='Unrecognised alert format'), 400

    raw_symbol    = signal['symbol']
    alpaca_symbol = CRYPTO_MAP.get(raw_symbol, raw_symbol)
    is_crypto     = raw_symbol in CRYPTO_MAP

    # Skip if already in a position for this symbol
    try:
        client.get_open_position(alpaca_symbol)
        log.info('Position already open for %s — skipping', alpaca_symbol)
        return jsonify(status='skipped', reason='position already open')
    except Exception:
        pass  # No existing position — proceed

    account = client.get_account()
    equity  = float(account.equity)
    side    = OrderSide.BUY if signal['side'] == 'LONG' else OrderSide.SELL

    if is_crypto:
        qty = round((equity * POSITION_PCT) / signal['entry'], 6)
        tif = TimeInForce.GTC
    else:
        qty = max(1, int((equity * POSITION_PCT) / signal['entry']))
        tif = TimeInForce.DAY

    log.info('Submitting %s %s %s  entry=%.4f  SL=%.4f  TP=%.4f',
             side.value.upper(), qty, alpaca_symbol,
             signal['entry'], signal['sl'], signal['tp'])

    try:
        if is_crypto:
            # Alpaca does not support bracket orders for crypto
            req = MarketOrderRequest(
                symbol        = alpaca_symbol,
                qty           = qty,
                side          = side,
                time_in_force = tif,
            )
        else:
            req = MarketOrderRequest(
                symbol        = alpaca_symbol,
                qty           = qty,
                side          = side,
                time_in_force = tif,
                order_class   = OrderClass.BRACKET,
                take_profit   = TakeProfitRequest(limit_price=round(signal['tp'], 2)),
                stop_loss     = StopLossRequest(stop_price=round(signal['sl'], 2)),
            )
        order = client.submit_order(req)
        log.info('Order submitted: %s', order.id)
        return jsonify(status='ok', order_id=str(order.id), qty=qty, symbol=alpaca_symbol)
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
