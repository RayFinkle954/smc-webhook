import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET", "test")

import webhook_server  # noqa: E402
import risk_manager  # noqa: E402
import incident_log  # noqa: E402
from alpaca.common.exceptions import APIError  # noqa: E402


class FakeAccount:
    def __init__(self, equity, last_equity=None):
        self.equity = str(equity)
        self.last_equity = str(last_equity if last_equity is not None else equity)
        self.buying_power = str(equity)


class RiskStatusRouteTest(unittest.TestCase):
    """GET /risk/status: pure reporting, must never trade or write state."""

    def setUp(self):
        self.app = webhook_server.app.test_client()
        self.state_path = Path(__file__).parent / "_test_risk_state_route.json"
        if self.state_path.exists():
            self.state_path.unlink()
        risk_manager.STATE_PATH = self.state_path
        risk_manager._today = lambda: "2026-07-15"
        risk_manager._this_month = lambda: "2026-07"

    def tearDown(self):
        if self.state_path.exists():
            self.state_path.unlink()

    def test_status_route_shape_and_content(self):
        mock_client = MagicMock()
        mock_client.get_account.return_value = FakeAccount(equity=100000, last_equity=99000)
        amd = MagicMock(symbol="AMD", market_value="5000")
        mock_client.get_all_positions.return_value = [amd]

        with patch.object(webhook_server, 'client', mock_client):
            resp = self.app.get('/risk/status')

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['status'], 'ok')
        caps = data['caps']
        self.assertIn('daily_loss', caps)
        self.assertIn('monthly_loss', caps)
        self.assertIn('per_underlying', caps)
        self.assertIn('crypto_beta_bucket', caps)
        self.assertEqual(caps['daily_loss']['limit_pct'], -risk_manager.DAILY_LOSS_LIMIT)
        self.assertEqual(caps['monthly_loss']['limit_pct'], -risk_manager.MONTHLY_LOSS_LIMIT)
        self.assertEqual(caps['crypto_beta_bucket']['limit_pct'], risk_manager.CRYPTO_BETA_CAP)
        self.assertEqual(caps['per_underlying'][0]['symbol'], 'AMD')
        self.assertEqual(caps['per_underlying'][0]['limit_pct'], risk_manager.PER_UNDERLYING_LIMIT)

    def test_status_route_never_trades_or_writes_state_even_past_halt(self):
        mock_client = MagicMock()
        # -10% daily move: well past the daily halt threshold.
        mock_client.get_account.return_value = FakeAccount(equity=90000, last_equity=100000)
        mock_client.get_all_positions.return_value = []

        with patch.object(webhook_server, 'client', mock_client):
            resp = self.app.get('/risk/status')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.state_path.exists())
        mock_client.close_all_positions.assert_not_called()
        mock_client.submit_order.assert_not_called()
        mock_client.close_position.assert_not_called()
        mock_client.cancel_order_by_id.assert_not_called()


class RiskIncidentsRouteTest(unittest.TestCase):
    def setUp(self):
        incident_log._incidents.clear()
        self.app = webhook_server.app.test_client()

    def tearDown(self):
        incident_log._incidents.clear()

    def test_incidents_route_returns_newest_first(self):
        incident_log.record('kind_a', 'first')
        incident_log.record('kind_b', 'second')

        resp = self.app.get('/risk/incidents')

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['incidents'][0]['kind'], 'kind_b')
        self.assertEqual(data['incidents'][1]['kind'], 'kind_a')

    def test_empty_log_returns_empty_list(self):
        resp = self.app.get('/risk/incidents')
        self.assertEqual(resp.get_json()['incidents'], [])


class IncidentLoggingAtCallSitesTest(unittest.TestCase):
    """Confirms the existing block/close code paths append an incident
    without changing their existing behavior (same response, same reason)."""

    def setUp(self):
        incident_log._incidents.clear()

    def tearDown(self):
        incident_log._incidents.clear()

    def test_underlying_block_logs_incident(self):
        mock_client = MagicMock()
        mock_client.get_open_position.side_effect = Exception('no position')

        with patch.object(webhook_server, 'client', mock_client), \
             patch.object(risk_manager, 'check_book_risk', return_value=(True, 'ok')), \
             patch.object(risk_manager, 'check_underlying_exposure', return_value=(False, 'AMD over cap')):
            mock_client.get_account.return_value = FakeAccount(equity=100000)
            resp = webhook_server.app.test_client().post(
                '/webhook',
                data='LONG | AMD | Entry: 100 | SL: 95 | TP: 110',
                content_type='text/plain',
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['reason'], 'AMD over cap')
        incidents = incident_log.get_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]['kind'], 'underlying_block')
        self.assertEqual(incidents[0]['detail'], 'AMD over cap')

    def test_crypto_beta_block_logs_incident(self):
        mock_client = MagicMock()
        mock_client.get_open_position.side_effect = Exception('no position')

        with patch.object(webhook_server, 'client', mock_client), \
             patch.object(risk_manager, 'check_book_risk', return_value=(True, 'ok')), \
             patch.object(risk_manager, 'check_underlying_exposure', return_value=(True, 'ok')), \
             patch.object(risk_manager, 'check_crypto_beta_exposure', return_value=(False, 'bucket over cap')):
            mock_client.get_account.return_value = FakeAccount(equity=100000)
            resp = webhook_server.app.test_client().post(
                '/webhook',
                data='LONG | BTCUSD | BTCTREND | Entry: 100 | SL: 95 | TP: 110',
                content_type='text/plain',
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['reason'], 'bucket over cap')
        incidents = incident_log.get_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]['kind'], 'crypto_beta_block')

    def test_gap_check_close_logs_incident(self):
        mock_client = MagicMock()
        mock_data_client = MagicMock()
        closed_action = {'symbol': 'AMD', 'gap': 5.0, 'atr': 2.0, 'action': 'closed'}

        with patch.object(webhook_server, 'client', mock_client), \
             patch.object(webhook_server, 'data_client', mock_data_client), \
             patch.object(webhook_server.overnight_risk, 'check_positions', return_value=[closed_action]):
            resp = webhook_server.app.test_client().get('/risk/gap-check')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['actions'], [closed_action])
        incidents = incident_log.get_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]['kind'], 'gap_check_close')
        self.assertEqual(incidents[0]['symbol'], 'AMD')

    def test_gap_check_no_action_logs_nothing(self):
        mock_client = MagicMock()
        mock_data_client = MagicMock()

        with patch.object(webhook_server, 'client', mock_client), \
             patch.object(webhook_server, 'data_client', mock_data_client), \
             patch.object(webhook_server.overnight_risk, 'check_positions', return_value=[]):
            resp = webhook_server.app.test_client().get('/risk/gap-check')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(incident_log.get_incidents(), [])


class ShortNotAllowedTest(unittest.TestCase):
    """2026-07-23 incident: Alpaca rejected a new equity short with
    'account is not allowed to short' (error code 40310000), and the webhook's
    bare 500 response made TradingView retry the identical doomed request
    every ~5s (dozens of hits in the Render logs for one alert). This must
    degrade to a clean skip + incident log entry instead."""

    def setUp(self):
        incident_log._incidents.clear()

    def tearDown(self):
        incident_log._incidents.clear()

    def _short_rejected_client(self):
        mock_client = MagicMock()
        mock_client.get_open_position.side_effect = Exception('no position')
        mock_client.get_account.return_value = FakeAccount(equity=100000)
        mock_client.submit_order.side_effect = APIError(
            '{"code": 40310000, "message": "account is not allowed to short"}'
        )
        return mock_client

    def test_short_not_allowed_returns_clean_skip_not_500(self):
        mock_client = self._short_rejected_client()

        with patch.object(webhook_server, 'client', mock_client), \
             patch.object(risk_manager, 'check_book_risk', return_value=(True, 'ok')), \
             patch.object(risk_manager, 'check_underlying_exposure', return_value=(True, 'ok')), \
             patch.object(risk_manager, 'check_crypto_beta_exposure', return_value=(True, 'ok')):
            resp = webhook_server.app.test_client().post(
                '/webhook',
                data='SHORT | GAPGO | AAPL | Entry: 319.56 | SL: 323.18 | TP: 312.32',
                content_type='text/plain',
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['status'], 'skipped')
        self.assertEqual(data['reason'], 'account not allowed to short')

    def test_short_not_allowed_logs_incident(self):
        mock_client = self._short_rejected_client()

        with patch.object(webhook_server, 'client', mock_client), \
             patch.object(risk_manager, 'check_book_risk', return_value=(True, 'ok')), \
             patch.object(risk_manager, 'check_underlying_exposure', return_value=(True, 'ok')), \
             patch.object(risk_manager, 'check_crypto_beta_exposure', return_value=(True, 'ok')):
            webhook_server.app.test_client().post(
                '/webhook',
                data='SHORT | GAPGO | AAPL | Entry: 319.56 | SL: 323.18 | TP: 312.32',
                content_type='text/plain',
            )

        incidents = incident_log.get_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]['kind'], 'short_not_allowed')
        self.assertEqual(incidents[0]['symbol'], 'AAPL')

    def test_other_api_errors_still_return_500(self):
        """Only the specific 40310000 code degrades gracefully -- any other
        Alpaca rejection must still surface loudly as a real error."""
        mock_client = self._short_rejected_client()
        mock_client.submit_order.side_effect = APIError(
            '{"code": 40010001, "message": "insufficient buying power"}'
        )

        with patch.object(webhook_server, 'client', mock_client), \
             patch.object(risk_manager, 'check_book_risk', return_value=(True, 'ok')), \
             patch.object(risk_manager, 'check_underlying_exposure', return_value=(True, 'ok')), \
             patch.object(risk_manager, 'check_crypto_beta_exposure', return_value=(True, 'ok')):
            resp = webhook_server.app.test_client().post(
                '/webhook',
                data='SHORT | GAPGO | AAPL | Entry: 319.56 | SL: 323.18 | TP: 312.32',
                content_type='text/plain',
            )

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(incident_log.get_incidents(), [])


if __name__ == '__main__':
    unittest.main()
