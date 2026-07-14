import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).parent / "risk_state.json"

DAILY_LOSS_LIMIT = 0.03      # flatten + block new entries for the rest of the day
MONTHLY_LOSS_LIMIT = 0.08    # flatten + block new entries for the rest of the month
# Sanity backstop, not a sizing tool: must sit ABOVE the largest intended
# single position or it silently blocks all trading. Max designed size is
# BTCTREND 8% base x 1.2 streak multiplier = 9.6%, so 10% only trips on a
# genuinely wrong order. (Was 0.02 at first deploy — below the 3-8% base
# sizes, which would have blocked every entry; caught in review before any
# live signal hit it.)
PER_UNDERLYING_LIMIT = 0.10

# Symbols that live correlation analysis (2026-07-14, 6mo daily returns) shows
# are effectively ONE crypto bet: BTC/ETH/SOL are 89-92% pairwise-correlated,
# MSTR is 80% to BTC, COIN 72%, CRCL ~55%. The per-underlying cap can't see
# this — a broad crypto drawdown hits all of them at once. Both slash and
# no-slash forms included (orders use BTC/USD, the positions API uses BTCUSD).
CRYPTO_BETA_SYMBOLS = {
    'BTC/USD', 'ETH/USD', 'SOL/USD', 'BTCUSD', 'ETHUSD', 'SOLUSD',
    'COIN', 'MSTR', 'CRCL',
}
# Backstop, not a sizing tool — same rule as PER_UNDERLYING_LIMIT: must sit
# ABOVE the summed base sizes of every crypto-linked sleeve (BTCTREND 8 +
# ETHTREND 6 + SOLTREND 7 + XEMAX2 2.5 + SMC COIN 3 + SMC MSTR 3 + EMAPB CRCL
# 3 = 32.5%) or it silently blocks configured strategies — the exact failure
# mode of the original 2% cap. It only trips on multiplier stacking or a
# wrongly-sized order. Covered by a regression test against the real config.
CRYPTO_BETA_CAP = 0.35

# NOTE: Render's filesystem is ephemeral — this state resets on every deploy/restart.
# Acceptable today since deploys are infrequent, but a halt could theoretically be
# "forgotten" by a redeploy that happens mid-halt. Revisit if that becomes a real risk.


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("risk_state.json unreadable, starting fresh")
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state))


def _market_now() -> datetime:
    """Halt windows are defined in market time: a daily halt should last until
    midnight ET, not midnight UTC (which is 8pm ET — early enough for the
    24/7 crypto sleeves to re-enter the same evening). Falls back to UTC on
    hosts without a tz database (e.g. Windows without the tzdata package)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc)


def _today() -> str:
    return _market_now().strftime("%Y-%m-%d")


def _this_month() -> str:
    return _market_now().strftime("%Y-%m")


def flatten_all(client, reason: str) -> None:
    log.warning("FLATTENING ALL POSITIONS — %s", reason)
    try:
        results = client.close_all_positions(cancel_orders=True)
        log.warning("Flatten complete: %d position(s) closed", len(results))
    except Exception as e:
        log.error("Flatten failed: %s", e)


def check_book_risk(client) -> tuple[bool, str]:
    """Call once per incoming webhook, before submitting any new entry order.

    Returns (allowed, reason). When not allowed, the caller should skip the
    order but still let CLOSE signals through (reducing risk is always fine).
    Flattens positions and persists a halt the moment a limit is breached, so
    a single breach only triggers one round of flattening, not one per alert.
    """
    state = _load_state()
    today = _today()
    month = _this_month()

    account = client.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity) if account.last_equity else equity

    # Roll monthly baseline forward on the first check of a new month.
    if state.get("month_start_month") != month:
        state["month_start_month"] = month
        state["month_start_equity"] = equity
        state.pop("monthly_halt_month", None)

    if state.get("daily_halt_date") == today:
        _save_state(state)
        return False, "daily loss limit already hit today — no new entries until tomorrow"

    if state.get("monthly_halt_month") == month:
        _save_state(state)
        return False, "monthly loss limit already hit this month — halted pending review"

    daily_pnl_pct = (equity - last_equity) / last_equity if last_equity else 0.0
    if daily_pnl_pct <= -DAILY_LOSS_LIMIT:
        flatten_all(client, f"daily loss {daily_pnl_pct:.2%} <= -{DAILY_LOSS_LIMIT:.0%}")
        state["daily_halt_date"] = today
        _save_state(state)
        return False, f"daily loss limit hit ({daily_pnl_pct:.2%}) — flattened, blocked for the rest of today"

    month_start_equity = state.get("month_start_equity", equity)
    monthly_pnl_pct = (equity - month_start_equity) / month_start_equity if month_start_equity else 0.0
    if monthly_pnl_pct <= -MONTHLY_LOSS_LIMIT:
        flatten_all(client, f"monthly loss {monthly_pnl_pct:.2%} <= -{MONTHLY_LOSS_LIMIT:.0%}")
        state["monthly_halt_month"] = month
        _save_state(state)
        return False, f"monthly loss limit hit ({monthly_pnl_pct:.2%}) — flattened, halted pending review"

    _save_state(state)
    return True, "ok"


def check_underlying_exposure(client, alpaca_symbol: str, new_notional: float, equity: float) -> tuple[bool, str]:
    """Notional-exposure cap per symbol, as a fraction of equity. This is a
    simplification of the "2% risk per underlying" idea from the strategy
    review — it caps position size (qty * price), not stop-distance-adjusted
    risk, because the latter would require tracking every open position's SL
    across strategies. Revisit if a strategy starts using much wider stops
    than the others, since notional alone would understate its real risk."""
    try:
        position = client.get_open_position(alpaca_symbol.replace("/", ""))
        current_notional = abs(float(position.market_value)) if position.market_value else 0.0
    except Exception:
        current_notional = 0.0

    projected = current_notional + new_notional
    limit = equity * PER_UNDERLYING_LIMIT
    if projected > limit:
        return False, f"{alpaca_symbol} exposure would be ${projected:,.0f}, over the ${limit:,.0f} per-underlying cap"
    return True, "ok"


def check_crypto_beta_exposure(client, alpaca_symbol: str, new_notional: float, equity: float) -> tuple[bool, str]:
    """Caps combined notional across the crypto-beta bucket (crypto sleeves +
    their equity proxies). Only consulted for orders on bucket symbols; fails
    open on API errors so a data hiccup never blocks trading outright."""
    if alpaca_symbol not in CRYPTO_BETA_SYMBOLS and alpaca_symbol.replace("/", "") not in CRYPTO_BETA_SYMBOLS:
        return True, "ok"

    bucket_notional = 0.0
    try:
        for position in client.get_all_positions():
            if position.symbol in CRYPTO_BETA_SYMBOLS or position.symbol.replace("/", "") in CRYPTO_BETA_SYMBOLS:
                bucket_notional += abs(float(position.market_value))
    except Exception as e:
        log.error("Crypto-beta bucket check failed open: %s", e)
        return True, "ok"

    projected = bucket_notional + new_notional
    limit = equity * CRYPTO_BETA_CAP
    if projected > limit:
        return False, (
            f"crypto-beta bucket (BTC/ETH/SOL + COIN/MSTR/CRCL) would be "
            f"${projected:,.0f}, over the ${limit:,.0f} cap ({CRYPTO_BETA_CAP:.0%} of equity)"
        )
    return True, "ok"
