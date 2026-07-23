import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ALPACA_API_KEY", "test")   # webhook_server (imported by the
os.environ.setdefault("ALPACA_SECRET", "test")    # base-size regression test) needs these
import risk_manager  # noqa: E402
import incident_log  # noqa: E402


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
        incident_log._incidents.clear()
        allowed, reason = risk_manager.check_book_risk(client)
        self.assertFalse(allowed)
        client.close_all_positions.assert_called_once_with(cancel_orders=True)

        # The trip must be logged exactly once, as a book_halt_daily incident.
        incidents = incident_log.get_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]['kind'], 'book_halt_daily')

        # A second alert later the same day must not flatten again, just stay blocked.
        client.close_all_positions.reset_mock()
        allowed2, reason2 = risk_manager.check_book_risk(client)
        self.assertFalse(allowed2)
        client.close_all_positions.assert_not_called()
        # ...and must not log a second incident for the same already-active halt.
        self.assertEqual(len(incident_log.get_incidents()), 1)
        incident_log._incidents.clear()

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
        incident_log._incidents.clear()
        client.get_account.return_value = FakeAccount(equity=91500, last_equity=91600)  # daily -0.1%, monthly -8.5%
        allowed, reason = risk_manager.check_book_risk(client)
        self.assertFalse(allowed)
        client.close_all_positions.assert_called_once_with(cancel_orders=True)
        incidents = incident_log.get_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]['kind'], 'book_halt_monthly')
        incident_log._incidents.clear()

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
        pos.market_value = "35000"
        client.get_all_positions.return_value = [pos]
        allowed, reason = risk_manager.check_crypto_beta_exposure(
            client, "ETH/USD", new_notional=8000, equity=100000
        )
        self.assertFalse(allowed)  # 35000 + 8000 = 43000 > 37000 cap
        self.assertIn("crypto-beta", reason)

    def test_bucket_ignores_non_bucket_symbols(self):
        client = MagicMock()
        allowed, _ = risk_manager.check_crypto_beta_exposure(
            client, "AMZN", new_notional=50000, equity=100000
        )
        self.assertTrue(allowed)
        client.get_all_positions.assert_not_called()

    def test_bucket_counts_equity_proxies(self):
        """MSTR/COIN/CRCL/HOOD count toward the bucket — MSTR/COIN are 72-80%
        correlated to BTC (measured 2026-07-14); HOOD joined 2026-07-15 with
        its 6% ORB slot on 0.74-0.80 correlation to COIN (crypto-sector
        sentiment), so they're the same bet in a drawdown."""
        client = MagicMock()
        mstr = MagicMock()
        mstr.symbol = "MSTR"
        mstr.market_value = "20000"
        coin = MagicMock()
        coin.symbol = "COIN"
        coin.market_value = "12000"
        hood = MagicMock()
        hood.symbol = "HOOD"
        hood.market_value = "6000"
        client.get_all_positions.return_value = [mstr, coin, hood]
        allowed, _ = risk_manager.check_crypto_beta_exposure(
            client, "BTC/USD", new_notional=8000, equity=100000
        )
        self.assertFalse(allowed)  # 38000 proxies + 8000 = 46000 > 37000

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
        # XEMAX2 removed 2026-07-23 (killed). NOTE: BTCMOM (added 2026-07-22,
        # 8% base) was never added to this demo set either -- a pre-existing
        # gap, not something introduced by the XEMAX2 removal; left as-is and
        # flagged in the vault rather than expanded here, since this static
        # mirror may not need to match risk_manager's real (dynamic,
        # live-position-based) enforcement path exactly. See vault:
        # Validation/VAL-2026-07-23-xemax2-kill-and-replacement.md.
        crypto_strategies = {"BTCTREND", "ETHTREND", "SOLTREND"}
        summed = sum(pct for code, pct in webhook_server.POSITION_PCT_BY_STRATEGY.items()
                     if code in crypto_strategies)
        # SMC trades MSTR (COIN pulled 2026-07-14, PF 0.945 under v3 filter),
        # EMAPB trades CRCL, ORB trades HOOD (bucket member since 2026-07-15)
        # — one base slot each
        summed += webhook_server.POSITION_PCT_BY_STRATEGY["SMC"]
        summed += webhook_server.POSITION_PCT_BY_STRATEGY["EMAPB"]
        summed += webhook_server.POSITION_PCT_BY_STRATEGY["ORB"]
        self.assertLess(summed, risk_manager.CRYPTO_BETA_CAP,
                        f"configured crypto-linked base sizes sum to {summed:.1%}, "
                        f">= the {risk_manager.CRYPTO_BETA_CAP:.0%} bucket cap")


class BookRiskStatusTest(unittest.TestCase):
    """GET /risk/status must never call check_book_risk directly (that's an
    enforcement path that can flatten positions); it uses this read-only
    twin instead. These tests confirm it reads the same thresholds/state
    without ever calling close_all_positions or writing risk_state.json."""

    def setUp(self):
        self.state_path = Path(__file__).parent / "_test_risk_state_status.json"
        if self.state_path.exists():
            self.state_path.unlink()
        risk_manager.STATE_PATH = self.state_path
        risk_manager._today = lambda: "2026-07-15"
        risk_manager._this_month = lambda: "2026-07"

    def tearDown(self):
        if self.state_path.exists():
            self.state_path.unlink()

    def test_never_flattens_even_past_the_daily_threshold(self):
        client = MagicMock()
        client.get_account.return_value = FakeAccount(equity=96000, last_equity=100000)  # -4%, past the 3% cap
        status = risk_manager.get_book_risk_status(client)
        client.close_all_positions.assert_not_called()
        self.assertFalse(self.state_path.exists())  # no state write either
        self.assertEqual(status['daily']['limit_pct'], -risk_manager.DAILY_LOSS_LIMIT)
        self.assertGreater(status['daily']['pct_of_limit_used'], 1.0)
        self.assertFalse(status['daily']['halted'])  # not actually halted -- check_book_risk was never called

    def test_reports_zero_utilization_in_profit(self):
        client = MagicMock()
        client.get_account.return_value = FakeAccount(equity=101000, last_equity=100000)  # +1%
        status = risk_manager.get_book_risk_status(client)
        self.assertEqual(status['daily']['pct_of_limit_used'], 0.0)

    def test_reflects_an_already_active_halt(self):
        client = MagicMock()
        client.get_account.return_value = FakeAccount(equity=96900, last_equity=100000)
        risk_manager.check_book_risk(client)  # actually trips the halt this time
        incident_log._incidents.clear()

        status = risk_manager.get_book_risk_status(client)
        self.assertTrue(status['daily']['halted'])

    def test_halfway_to_daily_limit_reports_fifty_percent(self):
        client = MagicMock()
        client.get_account.return_value = FakeAccount(equity=98500, last_equity=100000)  # -1.5% == half of -3%
        status = risk_manager.get_book_risk_status(client)
        self.assertAlmostEqual(status['daily']['pct_of_limit_used'], 0.5, places=4)


class UnderlyingExposureStatusTest(unittest.TestCase):
    def test_reports_utilization_for_every_held_position(self):
        client = MagicMock()
        amd = MagicMock(symbol="AMD", market_value="8000")
        nvda = MagicMock(symbol="NVDA", market_value="3000")
        client.get_all_positions.return_value = [amd, nvda]

        results = risk_manager.get_underlying_exposure_status(client, equity=100000)

        self.assertEqual(len(results), 2)
        # Sorted descending by utilization: AMD (80% of the $10k cap) first.
        self.assertEqual(results[0]['symbol'], 'AMD')
        self.assertAlmostEqual(results[0]['pct_of_limit_used'], 0.8, places=4)
        self.assertEqual(results[0]['limit_pct'], risk_manager.PER_UNDERLYING_LIMIT)
        self.assertAlmostEqual(results[1]['pct_of_limit_used'], 0.3, places=4)

    def test_no_positions_returns_empty_list(self):
        client = MagicMock()
        client.get_all_positions.return_value = []
        self.assertEqual(risk_manager.get_underlying_exposure_status(client, equity=100000), [])

    def test_position_fetch_failure_returns_empty_not_raises(self):
        client = MagicMock()
        client.get_all_positions.side_effect = RuntimeError("API down")
        self.assertEqual(risk_manager.get_underlying_exposure_status(client, equity=100000), [])


class CryptoBetaStatusTest(unittest.TestCase):
    def test_sums_bucket_members_against_the_cap(self):
        client = MagicMock()
        btc = MagicMock(symbol="BTCUSD", market_value="20000")
        mstr = MagicMock(symbol="MSTR", market_value="5000")
        amzn = MagicMock(symbol="AMZN", market_value="9000")  # not in the bucket
        client.get_all_positions.return_value = [btc, mstr, amzn]

        status = risk_manager.get_crypto_beta_status(client, equity=100000)

        self.assertEqual(status['current_notional'], 25000.0)
        self.assertEqual(status['limit_pct'], risk_manager.CRYPTO_BETA_CAP)
        self.assertAlmostEqual(status['pct_of_limit_used'], 25000 / (100000 * risk_manager.CRYPTO_BETA_CAP), places=4)
        self.assertEqual({m['symbol'] for m in status['members']}, {'BTCUSD', 'MSTR'})

    def test_fails_open_on_api_error(self):
        client = MagicMock()
        client.get_all_positions.side_effect = RuntimeError("API down")
        status = risk_manager.get_crypto_beta_status(client, equity=100000)
        self.assertEqual(status['current_notional'], 0.0)
        self.assertEqual(status['members'], [])


if __name__ == "__main__":
    unittest.main()
