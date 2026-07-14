import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alpaca.trading.enums import AssetClass, PositionSide  # noqa: E402

import overnight_risk  # noqa: E402


def make_position(symbol, current_price, lastday_price, asset_class=AssetClass.US_EQUITY,
                  side=PositionSide.LONG):
    p = MagicMock()
    p.symbol = symbol
    p.current_price = str(current_price)
    p.lastday_price = str(lastday_price)
    p.asset_class = asset_class
    p.side = side
    return p


def make_bar(h, l, c):
    b = MagicMock()
    b.high, b.low, b.close = h, l, c
    return b


class WilderAtrTest(unittest.TestCase):
    def test_atr_of_flat_series_is_small(self):
        n = 20
        highs = [100.2] * n
        lows = [99.8] * n
        closes = [100.0] * n
        atr = overnight_risk._wilder_atr(highs, lows, closes, 14)
        self.assertAlmostEqual(atr, 0.4, places=4)

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(overnight_risk._wilder_atr([100], [99], [99.5], 14))


class CheckPositionsTest(unittest.TestCase):
    def test_large_adverse_gap_closes_long(self):
        trading_client = MagicMock()
        trading_client.get_all_positions.return_value = [make_position('AMD', current_price=80, lastday_price=100)]
        trading_client.get_orders.return_value = []
        data_client = MagicMock()
        bars = [make_bar(100.5, 99.5, 100) for _ in range(20)]  # ATR ~= 1.0, gap of 20 >> 1.5x ATR
        data_client.get_stock_bars.return_value.data = {'AMD': bars}

        actions = overnight_risk.check_positions(trading_client, data_client)

        trading_client.close_position.assert_called_once_with('AMD')
        self.assertEqual(actions[0]['action'], 'closed')

    def test_large_adverse_gap_closes_short(self):
        trading_client = MagicMock()
        trading_client.get_all_positions.return_value = [
            make_position('LLY', current_price=120, lastday_price=100, side=PositionSide.SHORT)
        ]
        trading_client.get_orders.return_value = []
        data_client = MagicMock()
        bars = [make_bar(100.5, 99.5, 100) for _ in range(20)]
        data_client.get_stock_bars.return_value.data = {'LLY': bars}

        actions = overnight_risk.check_positions(trading_client, data_client)

        trading_client.close_position.assert_called_once_with('LLY')
        self.assertEqual(actions[0]['action'], 'closed')

    def test_favorable_gap_leaves_long_open(self):
        """Regression for 2026-07-13: a winning META long that gapped UP 8%
        over the weekend was closed by the direction-blind version. A
        favorable gap is not a risk event — the bracket TP handles it."""
        trading_client = MagicMock()
        trading_client.get_all_positions.return_value = [make_position('META', current_price=120, lastday_price=100)]
        data_client = MagicMock()
        bars = [make_bar(100.5, 99.5, 100) for _ in range(20)]  # gap 20 >> 1.5x ATR, but favorable
        data_client.get_stock_bars.return_value.data = {'META': bars}

        actions = overnight_risk.check_positions(trading_client, data_client)

        trading_client.close_position.assert_not_called()
        data_client.get_stock_bars.assert_not_called()  # skipped before the ATR fetch
        self.assertEqual(actions, [])

    def test_favorable_gap_leaves_short_open(self):
        trading_client = MagicMock()
        trading_client.get_all_positions.return_value = [
            make_position('LEU', current_price=80, lastday_price=100, side=PositionSide.SHORT)
        ]
        data_client = MagicMock()

        actions = overnight_risk.check_positions(trading_client, data_client)

        trading_client.close_position.assert_not_called()
        self.assertEqual(actions, [])

    def test_open_bracket_legs_cancelled_before_close(self):
        """Regression: bracket TP/SL legs hold the position's qty — closing
        without cancelling them first gets rejected by Alpaca."""
        trading_client = MagicMock()
        trading_client.get_all_positions.return_value = [make_position('AMD', current_price=80, lastday_price=100)]
        leg = MagicMock()
        leg.id = 'leg-1'
        trading_client.get_orders.return_value = [leg]
        call_order = []
        trading_client.cancel_order_by_id.side_effect = lambda _id: call_order.append('cancel')
        trading_client.close_position.side_effect = lambda _sym: call_order.append('close')
        data_client = MagicMock()
        bars = [make_bar(100.5, 99.5, 100) for _ in range(20)]
        data_client.get_stock_bars.return_value.data = {'AMD': bars}

        overnight_risk.check_positions(trading_client, data_client)

        trading_client.cancel_order_by_id.assert_called_once_with('leg-1')
        self.assertEqual(call_order, ['cancel', 'close'])

    def test_carry_symbol_is_skipped_even_on_big_move(self):
        """BIL sheds its distribution on ex-div dates — that must never read
        as a gap-risk event."""
        trading_client = MagicMock()
        trading_client.get_all_positions.return_value = [
            make_position(overnight_risk.CARRY_SYMBOL, current_price=91.0, lastday_price=91.5)
        ]
        data_client = MagicMock()

        actions = overnight_risk.check_positions(trading_client, data_client)

        trading_client.close_position.assert_not_called()
        data_client.get_stock_bars.assert_not_called()
        self.assertEqual(actions, [])

    def test_small_gap_leaves_position_open(self):
        trading_client = MagicMock()
        trading_client.get_all_positions.return_value = [make_position('AMD', current_price=100.5, lastday_price=100)]
        data_client = MagicMock()
        bars = [make_bar(105, 95, 100) for _ in range(20)]  # ATR ~= 10, gap of 0.5 well within tolerance
        data_client.get_stock_bars.return_value.data = {'AMD': bars}

        actions = overnight_risk.check_positions(trading_client, data_client)

        trading_client.close_position.assert_not_called()
        self.assertEqual(actions, [])

    def test_crypto_positions_are_skipped(self):
        trading_client = MagicMock()
        trading_client.get_all_positions.return_value = [
            make_position('BTC/USD', current_price=70000, lastday_price=50000, asset_class=AssetClass.CRYPTO)
        ]
        data_client = MagicMock()

        actions = overnight_risk.check_positions(trading_client, data_client)

        trading_client.close_position.assert_not_called()
        data_client.get_stock_bars.assert_not_called()
        self.assertEqual(actions, [])

    def test_missing_price_data_is_skipped_not_crashed(self):
        trading_client = MagicMock()
        p = make_position('AMD', current_price=100, lastday_price=100)
        p.current_price = None
        trading_client.get_all_positions.return_value = [p]
        data_client = MagicMock()

        actions = overnight_risk.check_positions(trading_client, data_client)
        self.assertEqual(actions, [])

    def test_positions_fetch_failure_returns_empty_not_crash(self):
        trading_client = MagicMock()
        trading_client.get_all_positions.side_effect = RuntimeError("API down")
        data_client = MagicMock()
        self.assertEqual(overnight_risk.check_positions(trading_client, data_client), [])


if __name__ == "__main__":
    unittest.main()
