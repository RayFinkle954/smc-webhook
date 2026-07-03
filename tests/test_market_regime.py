import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import market_regime  # noqa: E402


def make_bar(o, h, l, c):
    bar = MagicMock()
    bar.open, bar.high, bar.low, bar.close = o, h, l, c
    return bar


class WilderAdxTest(unittest.TestCase):
    def test_strong_uptrend_gives_high_adx(self):
        n = 60
        highs = [100 + i * 1.5 + 0.3 for i in range(n)]
        lows = [100 + i * 1.5 - 0.3 for i in range(n)]
        closes = [100 + i * 1.5 for i in range(n)]
        adx = market_regime._wilder_adx(highs, lows, closes, 14)
        self.assertIsNotNone(adx)
        self.assertGreater(adx, 30)

    def test_choppy_sideways_gives_low_adx(self):
        n = 60
        highs, lows, closes = [], [], []
        base = 100
        for i in range(n):
            wobble = 1.0 if i % 2 == 0 else -1.0
            closes.append(base + wobble)
            highs.append(base + wobble + 0.5)
            lows.append(base + wobble - 0.5)
        adx = market_regime._wilder_adx(highs, lows, closes, 14)
        self.assertIsNotNone(adx)
        self.assertLess(adx, 20)

    def test_insufficient_data_returns_none(self):
        adx = market_regime._wilder_adx([100, 101], [99, 100], [99.5, 100.5], 14)
        self.assertIsNone(adx)


class RegimeMultiplierTest(unittest.TestCase):
    def setUp(self):
        market_regime._cache["adx"] = None
        market_regime._cache["fetched_at"] = 0

    def test_low_adx_halves_size(self):
        market_regime._fetch_adx = lambda client: 15.0
        self.assertEqual(market_regime.get_regime_multiplier(MagicMock()), 0.5)

    def test_mid_adx_gives_80pct(self):
        market_regime._cache["adx"] = None
        market_regime._fetch_adx = lambda client: 25.0
        self.assertEqual(market_regime.get_regime_multiplier(MagicMock()), 0.8)

    def test_high_adx_gives_full_size(self):
        market_regime._cache["adx"] = None
        market_regime._fetch_adx = lambda client: 40.0
        self.assertEqual(market_regime.get_regime_multiplier(MagicMock()), 1.0)

    def test_fetch_failure_fails_open_to_full_size(self):
        market_regime._cache["adx"] = None

        def boom(client):
            raise RuntimeError("data API down")
        market_regime._fetch_adx = boom
        self.assertEqual(market_regime.get_regime_multiplier(MagicMock()), 1.0)

    def test_result_is_cached_within_ttl(self):
        calls = {"n": 0}

        def counting_fetch(client):
            calls["n"] += 1
            return 15.0
        market_regime._fetch_adx = counting_fetch
        market_regime.get_regime_multiplier(MagicMock())
        market_regime.get_regime_multiplier(MagicMock())
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
