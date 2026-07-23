import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# webhook_server (imported inside the reserve tests) requires these at import
# time; default them like tests/test_risk_manager.py does so this file runs
# without a .env (fresh clone, CI).
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET", "test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cash_carry  # noqa: E402
import incident_log  # noqa: E402


def make_client(equity, cash, clock_open=True, pending_orders=(), carry_market_value=None,
                position_symbols=()):
    client = MagicMock()
    client.get_clock.return_value.is_open = clock_open
    account = MagicMock()
    account.equity = str(equity)
    account.cash = str(cash)
    client.get_account.return_value = account
    client.get_orders.return_value = list(pending_orders)
    if carry_market_value is None:
        client.get_open_position.side_effect = RuntimeError('position does not exist')
    else:
        position = MagicMock()
        position.market_value = str(carry_market_value)
        client.get_open_position.return_value = position
    positions = []
    for symbol in position_symbols:
        pos = MagicMock()
        pos.symbol = symbol
        positions.append(pos)
    client.get_all_positions.return_value = positions
    return client


class CashCarryBuyTest(unittest.TestCase):
    def test_below_threshold_does_nothing(self):
        client = make_client(equity=100000, cash=15000)  # 15%
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'none')
        client.submit_order.assert_not_called()

    def test_above_threshold_deploys_excess_minus_buffer(self):
        client = make_client(equity=100000, cash=30000)  # 30%
        order = MagicMock()
        order.id = 'abc-123'
        client.submit_order.return_value = order

        result = cash_carry.rebalance_idle_cash(client)

        self.assertEqual(result['action'], 'bought')
        self.assertEqual(result['symbol'], 'BIL')
        # 30000 cash - 10000 buffer (10% of 100000 equity) = 20000 deployable
        self.assertEqual(result['notional'], 20000.0)
        client.submit_order.assert_called_once()
        submitted_req = client.submit_order.call_args[0][0]
        self.assertEqual(submitted_req.symbol, 'BIL')
        self.assertEqual(submitted_req.notional, 20000.0)

    def test_zero_equity_does_not_crash(self):
        client = make_client(equity=0, cash=0)
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'none')
        client.submit_order.assert_not_called()

    def test_order_failure_reports_failed_not_raises(self):
        client = make_client(equity=100000, cash=30000)
        client.submit_order.side_effect = RuntimeError("rejected")
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'failed')


class WeekendDoubleBuyRegressionTest(unittest.TestCase):
    """Regression for the 2026-07-04/05 incident: the cron ran Sat + Sun with
    the market closed, queued a full-size DAY buy each run (account.cash only
    decrements on fill), and both filled Monday — $164.5K BIL on $100K equity."""

    def test_market_closed_never_orders_even_with_idle_cash(self):
        client = make_client(equity=100000, cash=94700, clock_open=False)
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'none')
        self.assertIn('closed', result['reason'])
        client.submit_order.assert_not_called()

    def test_pending_carry_order_blocks_second_buy(self):
        queued = MagicMock()
        client = make_client(equity=100000, cash=94700, pending_orders=[queued])
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'none')
        self.assertIn('pending', result['reason'])
        client.submit_order.assert_not_called()

    def test_clock_fetch_failure_fails_safe(self):
        client = make_client(equity=100000, cash=94700)
        client.get_clock.side_effect = RuntimeError('API down')
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'none')
        client.submit_order.assert_not_called()


class SellBackTest(unittest.TestCase):
    def test_negative_cash_sells_shortfall_back(self):
        # The exact post-incident account state on 2026-07-14: leveraged BIL,
        # cash deeply negative. Sell-back must restore cash to the 10% buffer.
        client = make_client(equity=100227.32, cash=-57456.30, carry_market_value=164586.20)
        order = MagicMock()
        order.id = 'sell-1'
        client.submit_order.return_value = order

        result = cash_carry.rebalance_idle_cash(client)

        self.assertEqual(result['action'], 'sold')
        # buffer 10022.73 - (-57456.30) = 67479.03 shortfall
        self.assertAlmostEqual(result['notional'], 67479.03, places=2)
        submitted_req = client.submit_order.call_args[0][0]
        self.assertEqual(str(submitted_req.side), str(cash_carry.OrderSide.SELL))

    def test_shortfall_beyond_position_liquidates_all(self):
        client = make_client(equity=100000, cash=-5000, carry_market_value=8000)
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'sold_all')
        client.close_position.assert_called_once_with('BIL')
        client.submit_order.assert_not_called()

    def test_no_carry_position_means_nothing_to_sell(self):
        client = make_client(equity=100000, cash=5000)  # below buffer, no BIL
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'none')
        client.submit_order.assert_not_called()
        client.close_position.assert_not_called()

    def test_tiny_buffer_drift_does_not_churn(self):
        client = make_client(equity=100000, cash=9950, carry_market_value=50000)  # $50 under buffer
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'none')
        client.submit_order.assert_not_called()


class CryptoReserveTest(unittest.TestCase):
    """The crypto-reserve-aware buffer (2026-07-14 incident: Alpaca crypto
    buys need SETTLED cash; Friday-20:00-ET trend entries can't be funded by
    a same-day BIL sale). Reserve = worst-case entry size (base x max streak
    upsize x fill-drift margin) for every flat crypto sleeve, grouped by
    shared position symbol; the buffer widens to cover it."""

    @staticmethod
    def _need(pct):
        return pct * cash_carry.MAX_ENTRY_UPSIZE * cash_carry.CRYPTO_RESERVE_SAFETY

    def test_reserve_covers_streak_upsizing(self):
        """Regression (1.05x-vs-1.2x gap): the reserve must cover the largest
        multiplier streak_analysis can actually apply to an entry."""
        import streak_analysis
        self.assertGreaterEqual(cash_carry.MAX_ENTRY_UPSIZE, streak_analysis.MULT_HOT)

    def test_no_sizing_dict_means_no_reserve(self):
        client = make_client(equity=100000, cash=30000)
        client.submit_order.return_value = MagicMock(id='buy-1')
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['crypto_reserve_pct'], 0.0)
        self.assertEqual(result['buffer_pct'], cash_carry.CASH_BUFFER)
        self.assertEqual(result['notional'], 20000.0)  # legacy behavior intact
        client.get_all_positions.assert_not_called()

    def test_real_config_all_flat_reserves_and_sells_back(self):
        """House rule: exercise the REAL sizing config. With every crypto
        sleeve flat, the buffer must widen well past 10% and a 10%-cash book
        must sell BIL back to build the settled reserve."""
        import webhook_server
        sizing = webhook_server.POSITION_PCT_BY_STRATEGY
        # Config linkage: every reserve sleeve must exist in the live sizing
        # dict, or a rename would silently zero its reserve.
        for code in cash_carry.CRYPTO_SLEEVE_SYMBOLS:
            self.assertIn(code, sizing, f'{code} missing from POSITION_PCT_BY_STRATEGY')

        expected_reserve = sum(
            self._need(sizing[code]) for code in cash_carry.CRYPTO_SLEEVE_SYMBOLS
        )
        self.assertGreater(expected_reserve, cash_carry.CASH_BUFFER,
                           'reserve should exceed the base buffer when all sleeves are flat')

        client = make_client(equity=100000, cash=10000, carry_market_value=80000)
        client.submit_order.return_value = MagicMock(id='sell-1')
        result = cash_carry.rebalance_idle_cash(client, sizing)
        self.assertEqual(result['action'], 'sold')
        self.assertAlmostEqual(result['notional'],
                               round(100000 * expected_reserve - 10000, 2), places=2)
        self.assertAlmostEqual(result['crypto_reserve_pct'], round(expected_reserve, 4))

    def test_reserve_shrinks_when_crypto_sleeves_are_deployed(self):
        """All three trend symbols held: ETH/SOL sleeves are proven deployed
        (unique symbols), but the BTC group (BTCTREND+XEMAX2+BTCMOM) keeps its
        worst-case flat member reserved since positions aren't attributed
        per strategy. 21% cash sits inside the no-action band (above the
        now-larger 3-member buffer of ~20.16%, but not past the
        buffer+hysteresis deploy threshold of ~22.16%) -> no action.
        (BTCMOM joining the group raised the worst-case reserve past the 15%
        this test originally used, so the cash level was retuned to keep
        testing the "cash already covers the buffer, nothing to deploy yet"
        branch rather than incidentally flipping to 'sold' or 'bought'.)"""
        import webhook_server
        sizing = webhook_server.POSITION_PCT_BY_STRATEGY
        client = make_client(equity=100000, cash=21000, carry_market_value=60000,
                             position_symbols=('BTCUSD', 'ETHUSD', 'SOLUSD', 'BIL'))
        result = cash_carry.rebalance_idle_cash(client, sizing)
        btc_group = [self._need(sizing['BTCTREND']), self._need(sizing['XEMAX2']), self._need(sizing['BTCMOM'])]
        expected = sum(btc_group) - min(btc_group)
        self.assertEqual(result['action'], 'none')
        self.assertAlmostEqual(result['crypto_reserve_pct'], round(expected, 4))
        client.submit_order.assert_not_called()

    def test_slash_position_symbols_count_as_held(self):
        """Positions can come back as BTC/USD or BTCUSD depending on the API
        path — both forms must mark the sleeve as deployed (else the reserve
        would stay at the full all-flat level)."""
        import webhook_server
        sizing = webhook_server.POSITION_PCT_BY_STRATEGY
        client = make_client(equity=100000, cash=15000, carry_market_value=60000,
                             position_symbols=('BTC/USD', 'ETH/USD', 'SOL/USD'))
        result = cash_carry.rebalance_idle_cash(client, sizing)
        btc_group = [self._need(sizing['BTCTREND']), self._need(sizing['XEMAX2']), self._need(sizing['BTCMOM'])]
        self.assertAlmostEqual(result['crypto_reserve_pct'],
                               round(sum(btc_group) - min(btc_group), 4))

    def test_shared_symbol_still_reserves_group_worst_case(self):
        """Regression: one sleeve holding BTC must NOT zero the OTHER BTC
        sleeve's reserve — a held shared symbol proves only that some group
        member is deployed, so everything but the smallest stays reserved."""
        import webhook_server
        sizing = webhook_server.POSITION_PCT_BY_STRATEGY
        client = make_client(equity=100000, cash=10000, carry_market_value=80000,
                             position_symbols=('BTCUSD',))
        client.submit_order.return_value = MagicMock(id='sell-1')
        result = cash_carry.rebalance_idle_cash(client, sizing)
        btc_group = [self._need(sizing['BTCTREND']), self._need(sizing['XEMAX2']), self._need(sizing['BTCMOM'])]
        eth_sol = self._need(sizing['ETHTREND']) + self._need(sizing['SOLTREND'])
        expected = sum(btc_group) - min(btc_group) + eth_sol
        self.assertEqual(result['action'], 'sold')
        self.assertAlmostEqual(result['crypto_reserve_pct'], round(expected, 4))
        # The essence of the bug: the reserve must be MORE than the flat
        # ETH/SOL sleeves alone — the BTC group's worst-case member counts.
        self.assertGreater(expected, eth_sol)

    def test_position_fetch_failure_skips_rebalance(self):
        """Regression: an API blip must NOT trigger a real BIL trade off a
        guessed reserve — the run is skipped and the next cron retries."""
        import webhook_server
        client = make_client(equity=100000, cash=10000, carry_market_value=80000)
        client.get_all_positions.side_effect = RuntimeError('API down')
        result = cash_carry.rebalance_idle_cash(client, webhook_server.POSITION_PCT_BY_STRATEGY)
        self.assertEqual(result['action'], 'none')
        self.assertIn('unknown state', result['reason'])
        client.submit_order.assert_not_called()

    def test_wide_buffer_gets_deploy_hysteresis_not_churn(self):
        """Cash just above a widened buffer must NOT deploy (inside the
        hysteresis band); cash well above it deploys down to the buffer."""
        import webhook_server
        sizing = webhook_server.POSITION_PCT_BY_STRATEGY
        buffer_pct = sum(
            self._need(sizing[code]) for code in cash_carry.CRYPTO_SLEEVE_SYMBOLS
        )

        inside_band = round(100000 * (buffer_pct + cash_carry.DEPLOY_HYSTERESIS / 2), 2)
        client = make_client(equity=100000, cash=inside_band, carry_market_value=60000)
        result = cash_carry.rebalance_idle_cash(client, sizing)
        self.assertEqual(result['action'], 'none')
        client.submit_order.assert_not_called()

        well_above = 100000 * (buffer_pct + 0.10)
        client = make_client(equity=100000, cash=well_above, carry_market_value=60000)
        client.submit_order.return_value = MagicMock(id='buy-1')
        result = cash_carry.rebalance_idle_cash(client, sizing)
        self.assertEqual(result['action'], 'bought')
        self.assertAlmostEqual(result['notional'], round(well_above - 100000 * buffer_pct, 2),
                               places=2)


class RunawaySweepAlarmTest(unittest.TestCase):
    def setUp(self):
        incident_log._incidents.clear()

    def tearDown(self):
        incident_log._incidents.clear()

    def test_alarm_fires_at_incident_levels(self):
        # The real 7/6-7/14 state: $164.5K BIL on $100K equity (164% > 120% limit)
        client = make_client(equity=100227.32, cash=-57456.30, carry_market_value=164586.20)
        client.submit_order.return_value = MagicMock(id='sell-1')
        result = cash_carry.rebalance_idle_cash(client)
        self.assertIn('alarm', result)
        self.assertIn('164', result['alarm'])

        # The alarm must also be logged as an incident for /risk/incidents.
        incidents = incident_log.get_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]['kind'], 'cash_carry_alarm')
        self.assertIn('164', incidents[0]['detail'])

    def test_no_alarm_at_normal_carry_levels(self):
        client = make_client(equity=100000, cash=30000, carry_market_value=60000)  # 60% of equity
        client.submit_order.return_value = MagicMock(id='buy-1')
        result = cash_carry.rebalance_idle_cash(client)
        self.assertNotIn('alarm', result)
        self.assertEqual(incident_log.get_incidents(), [])


if __name__ == "__main__":
    unittest.main()
