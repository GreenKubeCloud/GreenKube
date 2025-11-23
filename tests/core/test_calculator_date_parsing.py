import unittest
from datetime import datetime

from greenkube.core.calculator import _to_datetime


class TestCalculatorDateParsing(unittest.TestCase):
    def test_to_datetime_raises_on_invalid_date(self):
        invalid_date = "invalid-date-string"
        # Currently this fails because it returns now() instead of raising
        with self.assertRaises(ValueError):
            _to_datetime(invalid_date)

    def test_to_datetime_valid(self):
        valid_date = "2023-10-23T12:00:00Z"
        dt = _to_datetime(valid_date)
        self.assertIsInstance(dt, datetime)
