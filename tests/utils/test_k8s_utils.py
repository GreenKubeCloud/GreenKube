from decimal import Decimal
from unittest.mock import MagicMock

from greenkube.utils import k8s_utils


def test_parse_quantity_handles_none_numbers_invalid_and_all_suffixes():
    assert k8s_utils.parse_quantity(None) == Decimal(0)
    assert k8s_utils.parse_quantity(2) == Decimal(2)
    assert k8s_utils.parse_quantity(Decimal("1.5")) == Decimal("1.5")
    assert k8s_utils.parse_quantity("not-a-number") == Decimal(0)

    expected = {
        "1Ki": Decimal(1024),
        "1Mi": Decimal(1024**2),
        "1Gi": Decimal(1024**3),
        "1Ti": Decimal(1024**4),
        "1Pi": Decimal(1024**5),
        "1Ei": Decimal(1024**6),
        "1n": Decimal("0.000000001"),
        "1u": Decimal("0.000001"),
        "1m": Decimal("0.001"),
        "1k": Decimal(1000),
        "1M": Decimal(1000**2),
        "1G": Decimal(1000**3),
        "1T": Decimal(1000**4),
        "1P": Decimal(1000**5),
        "1E": Decimal(1000**6),
        "42": Decimal(42),
    }
    for quantity, value in expected.items():
        assert k8s_utils.parse_quantity(quantity) == value


def test_request_parsers_return_zero_for_empty_or_parse_errors(monkeypatch):
    assert k8s_utils.parse_cpu_request("") == 0
    assert k8s_utils.parse_memory_request(None) == 0
    assert k8s_utils.parse_storage_request(None) == 0

    monkeypatch.setattr(k8s_utils, "parse_quantity", MagicMock(side_effect=ValueError))
    assert k8s_utils.parse_cpu_request("1") == 0
    assert k8s_utils.parse_memory_request("1Gi") == 0
    assert k8s_utils.parse_storage_request("1Gi") == 0


def test_request_parsers_convert_valid_values():
    assert k8s_utils.parse_cpu_request("250m") == 250
    assert k8s_utils.parse_memory_request("2Mi") == 2 * 1024**2
    assert k8s_utils.parse_storage_request("3Gi") == 3 * 1024**3
