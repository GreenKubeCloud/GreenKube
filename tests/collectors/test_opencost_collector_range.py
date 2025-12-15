import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from greenkube.collectors.opencost_collector import OpenCostCollector


class TestOpenCostCollectorRange(unittest.TestCase):
    @patch("greenkube.utils.http_client.requests.Session.get")
    def test_collect_range_uses_correct_window(self, mock_get):
        # Arrange
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}
        mock_get.return_value = mock_response

        collector = OpenCostCollector()
        start = datetime(2023, 10, 23, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2023, 10, 24, 0, 0, 0, tzinfo=timezone.utc)

        # Act
        # We need to mock _resolve_url to avoid probing complexity in test
        with patch.object(collector, "_resolve_url", return_value="http://opencost"):
            collector.collect_range(start, end)

        # Assert
        # Check that requests.Session.get was called with the correct window parameter
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        expected_window = f"{start_ts},{end_ts}"

        # Filter calls to the API url
        api_calls = [call for call in mock_get.mock_calls if "http://opencost" in str(call)]
        self.assertTrue(len(api_calls) > 0, "No API calls to http://opencost found")

        # Check the params of the last call
        kwargs = api_calls[-1][2]
        self.assertEqual(kwargs["params"]["window"], expected_window)
