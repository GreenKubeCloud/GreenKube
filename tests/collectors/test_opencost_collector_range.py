import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from greenkube.collectors.opencost_collector import OpenCostCollector


class TestOpenCostCollectorRange(unittest.TestCase):
    @patch("greenkube.collectors.opencost_collector.requests.get")
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
        collector.collect_range(start, end)

        # Assert
        # Check that requests.get was called with the correct window parameter
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        expected_window = f"{start_ts},{end_ts}"

        # We need to find the call that matches the request
        # Since collect calls _resolve_url which might probe, we need to be careful.
        # However, in the test environment, _resolve_url might return None if not mocked properly,
        # or if we mock config.

        # Let's mock _resolve_url to avoid side effects
        with patch.object(collector, "_resolve_url", return_value="http://opencost"):
            collector.collect_range(start, end)

            # Verify the call to the API
            # mock_get might be called multiple times if probe is called, but here we mocked _resolve_url
            # so it should be called once by _fetch

            # Filter calls to the API url
            api_calls = [call for call in mock_get.mock_calls if "http://opencost" in str(call)]
            self.assertTrue(len(api_calls) > 0)

            # Check the params of the last call
            kwargs = api_calls[-1][2]
            self.assertEqual(kwargs["params"]["window"], expected_window)
