import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import risk_manager  # noqa: E402


class FakeAccount:
    def __init__(self, equity, last_equity):
        self.equity = str(equity)
        self.last_equity = str(last_equity)


class FakePosition:
    def __init__(self, market_value):
        self.market_value = str(market_value)


class RiskManagerTest(unittest.TestCase):
    def setUp(self):
        self.state_path = Path(__file__).parent / "_test_risk_state.json"
        if self.state_path.exists():
            self.state_path.unlink()
        risk_manager.STATE_PATH = self.state_path
        risk_manager._today = lambda: "2026-07-03"
        risk_manager._this_month = lambda: "2026-07"

    def tearDown(self):
        if self.state_path.exists():
            self.state_path.unlink()

    def test_normal_day_allows_and_seeds_month_baseline(self):
        client = MagicMock()
        client.get_account.return_value = FakeAccount(equity=100000, last_equity=99900)
        allowed, reason = risk_manager.check_book_risk(client)
        self.assertTrue(allowed)
        client.close_all_positions.assert_not_called()

    def test_daily_loss_limit_flattens_and_blocks(self):
        client = MagicMock()
        client.get_account.return_value = FakeAccount(equity=96900, last_equity=100000)  # -3.1%
        allowed, reason = risk_manager.check_book_risk(client)
        self.assertFalse(allowed)
        client.close_all_positions.assert_called_once_with(cancel_orders=True)

        # A second alert later the same day must not flatten again, just stay blocked.
        client.close_all_positions.reset_mock()
        allowed2, reason2 = risk_manager.check_book_risk(client)
        self.assertFalse(allowed2)
        client.close_all_positions.assert_not_called()

    def test_daily_loss_just_under_limit_allows(self):
        client = MagicMock()
        client.get_account.return_value = FakeAccount(equity=97100, last_equity=100000)  # -2.9%
        allowed, _ = risk_manager.check_book_risk(client)
        self.assertTrue(allowed)
        client.close_all_positions.assert_not_called()

    def test_monthly_loss_limit_flattens_and_halts_for_month(self):
        client = MagicMock()
        # Seed month-start baseline at 100k with a neutral day first.
        client.get_account.return_value = FakeAccount(equity=100000, last_equity=100000)
        risk_manager.check_book_risk(client)

        # Now a big drop within daily tolerance but breaching the monthly limit.
        client.get_account.return_value = FakeAccount(equity=91500, last_equity=91600)  # daily -0.1%, monthly -8.5%
        allowed, reason = risk_manager.check_book_risk(client)
        self.assertFalse(allowed)
        client.close_all_positions.assert_called_once_with(cancel_orders=True)

    def test_month_rollover_resets_baseline(self):
        client = MagicMock()
        client.get_account.return_value = FakeAccount(equity=90000, last_equity=90000)
        risk_manager._this_month = lambda: "2026-06"
        risk_manager.check_book_risk(client)  # seeds June baseline at 90k, would be -10% vs a 100k July baseline

        risk_manager._this_month = lambda: "2026-07"
        client.get_account.return_value = FakeAccount(equity=89000, last_equity=89500)  # small daily move
        allowed, _ = risk_manager.check_book_risk(client)
        self.assertTrue(allowed)  # new month baseline is 89000, not carried over from June's 90000
        client.close_all_positions.assert_not_called()

    def test_underlying_exposure_blocks_over_cap(self):
        client = MagicMock()
        client.get_open_position.side_effect = Exception("no position")
        allowed, reason = risk_manager.check_underlying_exposure(
            client, "AMD", new_notional=2500, equity=100000
        )
        self.assertFalse(allowed)  # 2500 > 2% of 100000 = 2000

    def test_underlying_exposure_allows_under_cap(self):
        client = MagicMock()
        client.get_open_position.side_effect = Exception("no position")
        allowed, reason = risk_manager.check_underlying_exposure(
            client, "AMD", new_notional=1500, equity=100000
        )
        self.assertTrue(allowed)

    def test_underlying_exposure_accounts_for_existing_position(self):
        client = MagicMock()
        client.get_open_position.return_value = FakePosition(market_value=1000)
        allowed, reason = risk_manager.check_underlying_exposure(
            client, "AMD", new_notional=1500, equity=100000
        )
        self.assertFalse(allowed)  # 1000 existing + 1500 new = 2500 > 2000 cap


if __name__ == "__main__":
    unittest.main()
