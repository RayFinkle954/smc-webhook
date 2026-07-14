import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cash_carry  # noqa: E402


def make_client(equity, cash, clock_open=True, pending_orders=(), carry_market_value=None):
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


class RunawaySweepAlarmTest(unittest.TestCase):
    def test_alarm_fires_at_incident_levels(self):
        # The real 7/6-7/14 state: $164.5K BIL on $100K equity (164% > 120% limit)
        client = make_client(equity=100227.32, cash=-57456.30, carry_market_value=164586.20)
        client.submit_order.return_value = MagicMock(id='sell-1')
        result = cash_carry.rebalance_idle_cash(client)
        self.assertIn('alarm', result)
        self.assertIn('164', result['alarm'])

    def test_no_alarm_at_normal_carry_levels(self):
        client = make_client(equity=100000, cash=30000, carry_market_value=60000)  # 60% of equity
        client.submit_order.return_value = MagicMock(id='buy-1')
        result = cash_carry.rebalance_idle_cash(client)
        self.assertNotIn('alarm', result)


if __name__ == "__main__":
    unittest.main()
