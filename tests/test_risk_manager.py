import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ALPACA_API_KEY", "test")   # webhook_server (imported by the
os.environ.setdefault("ALPACA_SECRET", "test")    # base-size regression test) needs these
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
            client, "AMD", new_notional=10500, equity=100000
        )
        self.assertFalse(allowed)  # 10500 > 10% of 100000 = 10000

    def test_underlying_exposure_accounts_for_existing_position(self):
        client = MagicMock()
        client.get_open_position.return_value = FakePosition(market_value=6000)
        allowed, reason = risk_manager.check_underlying_exposure(
            client, "AMD", new_notional=5000, equity=100000
        )
        self.assertFalse(allowed)  # 6000 existing + 5000 new = 11000 > 10000 cap

    def test_every_configured_base_size_passes_the_cap(self):
        """Regression: the cap started life at 2%, BELOW the 3-8% per-strategy
        base sizes, which would have silently blocked every live entry. The
        cap must clear every configured base size even after the max streak
        multiplier (1.2x), or it stops being a backstop and becomes a wall."""
        import webhook_server
        client = MagicMock()
        client.get_open_position.side_effect = Exception("no position")
        for code, base_pct in webhook_server.POSITION_PCT_BY_STRATEGY.items():
            worst_case = base_pct * 1.2  # max streak multiplier
            allowed, reason = risk_manager.check_underlying_exposure(
                client, "TEST", new_notional=100000 * worst_case, equity=100000
            )
            self.assertTrue(allowed, f"{code} at {worst_case:.1%} blocked: {reason}")


class CryptoBetaBucketTest(unittest.TestCase):
    def test_bucket_blocks_over_cap(self):
        client = MagicMock()
        pos = MagicMock()
        pos.symbol = "BTCUSD"
        pos.market_value = "30000"
        client.get_all_positions.return_value = [pos]
        allowed, reason = risk_manager.check_crypto_beta_exposure(
            client, "ETH/USD", new_notional=8000, equity=100000
        )
        self.assertFalse(allowed)  # 30000 + 8000 = 38000 > 35000 cap
        self.assertIn("crypto-beta", reason)

    def test_bucket_ignores_non_bucket_symbols(self):
        client = MagicMock()
        allowed, _ = risk_manager.check_crypto_beta_exposure(
            client, "AMZN", new_notional=50000, equity=100000
        )
        self.assertTrue(allowed)
        client.get_all_positions.assert_not_called()

    def test_bucket_counts_equity_proxies(self):
        """MSTR/COIN/CRCL count toward the bucket — they're 55-80% correlated
        to BTC (measured 2026-07-14), so they're the same bet in a drawdown."""
        client = MagicMock()
        mstr = MagicMock()
        mstr.symbol = "MSTR"
        mstr.market_value = "20000"
        coin = MagicMock()
        coin.symbol = "COIN"
        coin.market_value = "12000"
        client.get_all_positions.return_value = [mstr, coin]
        allowed, _ = risk_manager.check_crypto_beta_exposure(
            client, "BTC/USD", new_notional=8000, equity=100000
        )
        self.assertFalse(allowed)  # 32000 proxies + 8000 = 40000 > 35000

    def test_bucket_fails_open_on_api_error(self):
        client = MagicMock()
        client.get_all_positions.side_effect = RuntimeError("API down")
        allowed, _ = risk_manager.check_crypto_beta_exposure(
            client, "BTC/USD", new_notional=8000, equity=100000
        )
        self.assertTrue(allowed)

    def test_cap_clears_all_configured_crypto_sleeves(self):
        """Regression (same failure mode as the original 2% cap): the bucket
        cap must sit above the SUM of every crypto-linked sleeve's base size,
        or fully-deployed configured strategies get silently blocked."""
        import webhook_server
        crypto_strategies = {"BTCTREND", "ETHTREND", "SOLTREND", "XEMAX2"}
        summed = sum(pct for code, pct in webhook_server.POSITION_PCT_BY_STRATEGY.items()
                     if code in crypto_strategies)
        # SMC trades MSTR (COIN pulled 2026-07-14, PF 0.945 under v3 filter),
        # EMAPB trades CRCL — one base slot each
        summed += webhook_server.POSITION_PCT_BY_STRATEGY["SMC"]
        summed += webhook_server.POSITION_PCT_BY_STRATEGY["EMAPB"]
        self.assertLess(summed, risk_manager.CRYPTO_BETA_CAP,
                        f"configured crypto-linked base sizes sum to {summed:.1%}, "
                        f">= the {risk_manager.CRYPTO_BETA_CAP:.0%} bucket cap")


if __name__ == "__main__":
    unittest.main()
