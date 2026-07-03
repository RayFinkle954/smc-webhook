import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import streak_analysis  # noqa: E402


def make_order(client_order_id, symbol, side, entry_price, exit_price, qty, t0):
    """A simple bracket order: entry fill + one filled leg (the exit)."""
    order = MagicMock()
    order.client_order_id = client_order_id
    order.symbol = symbol
    order.side = MagicMock(value=side)
    order.filled_avg_price = str(entry_price)
    order.filled_at = t0
    order.filled_qty = str(qty)
    order.qty = str(qty)

    leg = MagicMock()
    leg.status = 'filled'
    leg.filled_avg_price = str(exit_price)
    leg.filled_at = t0 + timedelta(hours=1)
    leg.filled_qty = str(qty)
    leg.qty = str(qty)
    order.legs = [leg]
    return order


class StreakAnalysisTest(unittest.TestCase):
    def setUp(self):
        streak_analysis._cache = {}

    def test_no_strategy_code_returns_1x(self):
        client = MagicMock()
        self.assertEqual(streak_analysis.get_streak_multiplier(client, None), 1.0)
        client.get_orders.assert_not_called()

    def test_fewer_than_window_trades_returns_1x(self):
        client = MagicMock()
        t0 = datetime(2026, 7, 1)
        orders = [make_order(f'SMC-AMD-{i}', 'AMD', 'buy', 100, 105, 10, t0 + timedelta(days=i)) for i in range(3)]
        client.get_orders.return_value = orders
        self.assertEqual(streak_analysis.get_streak_multiplier(client, 'SMC', window=5), 1.0)

    def test_winning_streak_scales_up(self):
        client = MagicMock()
        t0 = datetime(2026, 7, 1)
        # 5 winning long trades: bought at 100, sold at 110
        orders = [make_order(f'SMC-AMD-{i}', 'AMD', 'buy', 100, 110, 10, t0 + timedelta(days=i)) for i in range(5)]
        client.get_orders.return_value = orders
        self.assertEqual(streak_analysis.get_streak_multiplier(client, 'SMC', window=5), 1.2)

    def test_losing_streak_scales_down(self):
        client = MagicMock()
        t0 = datetime(2026, 7, 1)
        # 5 losing long trades: bought at 100, sold at 90
        orders = [make_order(f'SMC-AMD-{i}', 'AMD', 'buy', 100, 90, 10, t0 + timedelta(days=i)) for i in range(5)]
        client.get_orders.return_value = orders
        self.assertEqual(streak_analysis.get_streak_multiplier(client, 'SMC', window=5), 0.8)

    def test_mixed_results_stays_at_1x(self):
        client = MagicMock()
        t0 = datetime(2026, 7, 1)
        orders = [
            make_order('SMC-AMD-1', 'AMD', 'buy', 100, 110, 10, t0),
            make_order('SMC-AMD-2', 'AMD', 'buy', 100, 90, 10, t0 + timedelta(days=1)),
            make_order('SMC-AMD-3', 'AMD', 'buy', 100, 110, 10, t0 + timedelta(days=2)),
            make_order('SMC-AMD-4', 'AMD', 'buy', 100, 90, 10, t0 + timedelta(days=3)),
            make_order('SMC-AMD-5', 'AMD', 'buy', 100, 110, 10, t0 + timedelta(days=4)),
        ]
        client.get_orders.return_value = orders
        # 3/5 wins = 60%, not > 0.6, so stays neutral
        self.assertEqual(streak_analysis.get_streak_multiplier(client, 'SMC', window=5), 1.0)

    def test_only_matching_strategy_orders_are_counted(self):
        client = MagicMock()
        t0 = datetime(2026, 7, 1)
        smc_orders = [make_order(f'SMC-AMD-{i}', 'AMD', 'buy', 100, 110, 10, t0 + timedelta(days=i)) for i in range(5)]
        other_orders = [make_order(f'EMAPB-LLY-{i}', 'LLY', 'buy', 100, 90, 10, t0 + timedelta(days=i)) for i in range(5)]
        client.get_orders.return_value = smc_orders + other_orders
        self.assertEqual(streak_analysis.get_streak_multiplier(client, 'SMC', window=5), 1.2)

    def test_fetch_failure_fails_open_and_does_not_cache(self):
        client = MagicMock()
        client.get_orders.side_effect = RuntimeError("API down")
        self.assertEqual(streak_analysis.get_streak_multiplier(client, 'SMC'), 1.0)
        self.assertNotIn('SMC', streak_analysis._cache)

    def test_result_is_cached_within_ttl(self):
        client = MagicMock()
        t0 = datetime(2026, 7, 1)
        client.get_orders.return_value = [
            make_order(f'SMC-AMD-{i}', 'AMD', 'buy', 100, 110, 10, t0 + timedelta(days=i)) for i in range(5)
        ]
        streak_analysis.get_streak_multiplier(client, 'SMC', window=5)
        streak_analysis.get_streak_multiplier(client, 'SMC', window=5)
        self.assertEqual(client.get_orders.call_count, 1)


if __name__ == "__main__":
    unittest.main()
