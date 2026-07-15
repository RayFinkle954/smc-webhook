import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import incident_log  # noqa: E402


class IncidentLogTest(unittest.TestCase):
    def setUp(self):
        incident_log._incidents.clear()

    def tearDown(self):
        incident_log._incidents.clear()

    def test_record_and_read_back(self):
        incident_log.record('underlying_block', 'AMD over cap', symbol='AMD')
        incidents = incident_log.get_incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]['kind'], 'underlying_block')
        self.assertEqual(incidents[0]['detail'], 'AMD over cap')
        self.assertEqual(incidents[0]['symbol'], 'AMD')
        self.assertIn('ts', incidents[0])

    def test_newest_first(self):
        incident_log.record('kind_a', 'first')
        incident_log.record('kind_b', 'second')
        incidents = incident_log.get_incidents()
        self.assertEqual(incidents[0]['kind'], 'kind_b')
        self.assertEqual(incidents[1]['kind'], 'kind_a')

    def test_capped_at_max_incidents(self):
        for i in range(incident_log.MAX_INCIDENTS + 20):
            incident_log.record('kind', f'entry {i}')
        incidents = incident_log.get_incidents()
        self.assertEqual(len(incidents), incident_log.MAX_INCIDENTS)
        # Newest entry (last recorded) must be first, oldest ones dropped.
        self.assertEqual(incidents[0]['detail'], f'entry {incident_log.MAX_INCIDENTS + 19}')


if __name__ == '__main__':
    unittest.main()
