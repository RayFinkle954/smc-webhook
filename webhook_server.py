import os
import re
import time
import logging
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.common.exceptions import APIError
from dotenv import load_dotenv

import risk_manager
import market_regime
import streak_analysis
import overnight_risk
import cash_carry
import incident_log

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
    # XEMAX2 KILLED 2026-07-23, ahead of its scheduled 2026-08-03 renew-or-kill
    # review: the fuller live sample (13 closed trades) came back worse, not
    # better -- PF ~0.60, 15.4% WR, cumulative -$132. Its own current
    # TradingView backtest confirms this wasn't just live noise: PF 0.756/
    # 35.29% WR over the last ~4 months, and only 0.749-0.957 over its whole
    # available history back to 2020/2024 -- never a real edge, not a recent
    # regime shift. TradingView alert deleted same day. Entry removed here as
    # defense-in-depth (no alert means no new signals regardless). See vault:
    # Validation/VAL-2026-07-23-xemax2-kill-and-replacement.md,
    # Strategies/Crypto EMA Cross v2.md, Candidate Pipeline.md.
    # ORB raised 4% -> 6% on 2026-07-14: best live sleeve (PF 12.97, 75% WR
    # over its first 4 closed trades). Deliberately NOT the recommended 8%
    # yet -- small sample; go to 8% only after ~15 closed trades if the PF
    # holds. See vault: Sessions/Performance Review 4 + Execution Fixes.
    'ORB':      0.06,
    # GAPGO deployed 2026-07-14 on META + AAPL only (5m, paper-first): the
    # 27-week backtest re-run kept only those two (PF 7.8 / 7.5); NVDA and
    # TSLA collapsed below 1.0 and were not deployed. Signals are rare
    # (~1/month/ticker) -- start small, revisit after ~10 closed trades.
    'GAPGO':    0.03,
    'SOLTREND': 0.07,
    'BTCTREND': 0.08,
    'ETHTREND': 0.06,
    # BTCMOM deployed 2026-07-22: TIS - BTC ESTRATEGIA Momentum + Confluencia
    # (open-source TradingView community script, mariellangsaez), validated via
    # real Strategy Tester (BTCUSD 4h, Dec 2020-Jul 2026: 47 trades, PF 3.655,
    # Sharpe 0.257, CAGR 9.78%, max DD 6.83%). Sized equal to BTCTREND per
    # explicit user instruction -- a lower-return/lower-drawdown complementary
    # BTC sleeve, not a replacement. See vault: Algo Trading/_System/Candidate
    # Pipeline.md and Validation/VAL-2026-07-22-community-strategy-validation.md.
    'BTCMOM':   0.08,
    # BTCGX deployed 2026-07-24 as a paper-test replacement candidate for
    # XEMAX2's killed slot: classic 20/50 SMA daily golden-cross trend
    # follower. Real TradingView Strategy Tester, BTCUSD 1D, full history
    # 2011-2026 (fixed $100K notional per trade, non-compounding): 58 trades,
    # PF 10.09, 45.6% WR, max DD only 3.56%, both the 2011-2020 and 2020-2026
    # halves independently profitable. Sized conservatively at 3% (below
    # BTCTREND/BTCMOM's 8%) because it is unproven live and its most recent
    # 5 closed trades (Jan-Jul 2026) were all losses -- expected behavior for
    # a slow trend-follower during BTC's post-Oct-2025 topping/decline, not
    # necessarily a broken edge, but reason enough to size it small until it
    # proves out on real paper fills. See vault: Validation/
    # VAL-2026-07-23-xemax2-kill-and-replacement.md.
    'BTCGX':    0.03,
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
        incident_log.record('underlying_block', exposure_reason, symbol=alpaca_symbol)
        return jsonify(status='skipped', reason=exposure_reason)

    bucket_ok, bucket_reason = risk_manager.check_crypto_beta_exposure(
        client, alpaca_symbol, qty * signal['entry'], equity
    )
    if not bucket_ok:
        log.warning('Order blocked by crypto-beta bucket cap: %s', bucket_reason)
        incident_log.record('crypto_beta_block', bucket_reason, symbol=alpaca_symbol)
        return jsonify(status='skipped', reason=bucket_reason)

    if is_crypto and side == OrderSide.BUY and signal['strategy'] not in cash_carry.CRYPTO_SLEEVE_SYMBOLS:
        # Not a block: the order may still fill if settled cash happens to be
        # there. But no reserve is being maintained for this sleeve, so its
        # entries can fail on unsettled funds until it's registered in
        # cash_carry.CRYPTO_SLEEVE_SYMBOLS.
        log.error('Crypto sleeve %s missing from cash_carry.CRYPTO_SLEEVE_SYMBOLS — '
                  'no settled-cash reserve is maintained for it', signal['strategy'])

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
    except APIError as e:
        # 40310000 = "account is not allowed to short" -- a deterministic
        # account-permission rejection (e.g. shorting/margin disabled), not a
        # transient error. Returning 500 here made TradingView retry the exact
        # same doomed request every ~5s (2026-07-23 incident: a single GAPGO
        # AAPL short alert produced dozens of identical failures in the Render
        # logs). Acknowledge cleanly instead so TradingView stops retrying,
        # and log to incident_log so it surfaces in the dashboard rather than
        # only in raw server logs.
        try:
            error_code = e.code
        except Exception:
            error_code = None
        if side == OrderSide.SELL and error_code == 40310000:
            log.warning('Short entry rejected (account not allowed to short): %s %s', alpaca_symbol, e)
            incident_log.record('short_not_allowed', str(e), symbol=alpaca_symbol)
            return jsonify(status='skipped', reason='account not allowed to short', symbol=alpaca_symbol)
        log.error('Order failed: %s', e)
        return jsonify(error=str(e)), 500
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
    for a in actions:
        if a.get('action') == 'closed':
            incident_log.record(
                'gap_check_close',
                f"{a['symbol']} moved {a['gap']:.2f} vs ATR {a['atr']:.2f} — closed",
                symbol=a['symbol'],
            )
    return jsonify(status='ok', checked_at=time.time(), actions=actions)


@app.route('/risk/cash-carry', methods=['GET', 'POST'])
def cash_carry_check():
    """Deploys idle cash above the buffer into BIL (T-bills) once it exceeds
    the deploy threshold. The buffer is crypto-reserve-aware: passing the
    sizing dict lets cash_carry keep settled cash on hand for whichever
    crypto sleeves are currently flat (Alpaca crypto buys can't use T+1
    unsettled BIL proceeds — see the 2026-07-14 incident note in
    cash_carry.py). No internal scheduler — call from an external cron, e.g.
    once daily. See cash_carry.py for what was deliberately left out of scope
    (FX-carry ETFs, covered calls) and why."""
    result = cash_carry.rebalance_idle_cash(client, POSITION_PCT_BY_STRATEGY)
    return jsonify(status='ok', checked_at=time.time(), result=result)


@app.route('/risk/status', methods=['GET'])
def risk_status():
    """Read-only utilization snapshot against every live risk cap in
    risk_manager.py: daily/monthly loss-halt distance, per-underlying
    exposure for every held position, and the crypto-beta bucket total.
    Pure reporting -- never places, cancels, or modifies an order, and never
    writes state (see risk_manager.get_book_risk_status's docstring for why
    it deliberately does NOT call check_book_risk, which is an enforcement
    path that can flatten positions)."""
    account = client.get_account()
    equity = float(account.equity)

    book = risk_manager.get_book_risk_status(client)
    underlyings = risk_manager.get_underlying_exposure_status(client, equity)
    crypto_beta = risk_manager.get_crypto_beta_status(client, equity)

    return jsonify(
        status='ok',
        checked_at=time.time(),
        equity=equity,
        caps={
            'daily_loss': book['daily'],
            'monthly_loss': book['monthly'],
            'per_underlying': underlyings,
            'crypto_beta_bucket': crypto_beta,
        },
    )


@app.route('/risk/incidents', methods=['GET'])
def risk_incidents():
    """Recent risk-control trip events (book-level halts, per-underlying
    blocks, crypto-beta bucket blocks, gap-check adverse closes, cash-carry
    runaway alarms), newest first. In-memory only since the last
    deploy/restart -- see incident_log.py; Render's filesystem is ephemeral
    so nothing here is persisted to disk."""
    return jsonify(status='ok', checked_at=time.time(), incidents=incident_log.get_incidents())


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
