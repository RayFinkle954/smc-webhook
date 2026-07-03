import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cash_carry  # noqa: E402


def make_account(equity, cash):
    a = MagicMock()
    a.equity = str(equity)
    a.cash = str(cash)
    return a


class CashCarryTest(unittest.TestCase):
    def test_below_threshold_does_nothing(self):
        client = MagicMock()
        client.get_account.return_value = make_account(equity=100000, cash=15000)  # 15%
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'none')
        client.submit_order.assert_not_called()

    def test_above_threshold_deploys_excess_minus_buffer(self):
        client = MagicMock()
        client.get_account.return_value = make_account(equity=100000, cash=30000)  # 30%
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
        client = MagicMock()
        client.get_account.return_value = make_account(equity=0, cash=0)
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'none')
        client.submit_order.assert_not_called()

    def test_order_failure_reports_failed_not_raises(self):
        client = MagicMock()
        client.get_account.return_value = make_account(equity=100000, cash=30000)
        client.submit_order.side_effect = RuntimeError("rejected")
        result = cash_carry.rebalance_idle_cash(client)
        self.assertEqual(result['action'], 'failed')


if __name__ == "__main__":
    unittest.main()
