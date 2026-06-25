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

POSITION_PCT = 0.10  # 10% of equity per trade — matches strategy setting

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


@app.route('/webhook', methods=['POST'])
def webhook():
    body = request.get_data(as_text=True)
    log.info('Alert received: %s', body)

    signal = parse_alert(body)
    if not signal:
        log.warning('Could not parse alert: %s', body)
        return jsonify(error='Unrecognised alert format'), 400

    # Skip if already in a position for this symbol
    try:
        client.get_open_position(signal['symbol'])
        log.info('Position already open for %s — skipping', signal['symbol'])
        return jsonify(status='skipped', reason='position already open')
    except Exception:
        pass  # No existing position — proceed

    account = client.get_account()
    equity  = float(account.equity)
    qty     = max(1, int((equity * POSITION_PCT) / signal['entry']))
    side    = OrderSide.BUY if signal['side'] == 'LONG' else OrderSide.SELL

    log.info('Submitting %s %dx %s  entry=%.4f  SL=%.4f  TP=%.4f',
             side.value.upper(), qty, signal['symbol'],
             signal['entry'], signal['sl'], signal['tp'])

    req = MarketOrderRequest(
        symbol        = signal['symbol'],
        qty           = qty,
        side          = side,
        time_in_force = TimeInForce.DAY,
        order_class   = OrderClass.BRACKET,
        take_profit   = TakeProfitRequest(limit_price=round(signal['tp'], 4)),
        stop_loss     = StopLossRequest(stop_price=round(signal['sl'], 4)),
    )
    order = client.submit_order(req)
    log.info('Order submitted: %s', order.id)
    return jsonify(status='ok', order_id=str(order.id), qty=qty, symbol=signal['symbol'])


@app.route('/health', methods=['GET'])
def health():
    acct = client.get_account()
    return jsonify(status='ok', equity=acct.equity, buying_power=acct.buying_power)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
