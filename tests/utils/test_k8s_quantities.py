# tests/utils/test_k8s_quantities.py
"""Tests for Kubernetes resource quantity parsing."""

from greenkube.utils.k8s_quantities import parse_cpu_quantity, parse_memory_quantity


class TestParseCpuQuantity:
    def test_millicores(self):
        assert parse_cpu_quantity("250m") == 250

    def test_cores(self):
        assert parse_cpu_quantity("1") == 1000

    def test_fractional_cores(self):
        assert parse_cpu_quantity("0.5") == 500

    def test_nanocores(self):
        assert parse_cpu_quantity("1500000n") == 2

    def test_numeric_input(self):
        assert parse_cpu_quantity(2) == 2000

    def test_invalid(self):
        assert parse_cpu_quantity("not-a-quantity") is None

    def test_none(self):
        assert parse_cpu_quantity(None) is None


class TestParseMemoryQuantity:
    def test_mebibytes(self):
        assert parse_memory_quantity("512Mi") == 512 * 1024**2

    def test_gibibytes(self):
        assert parse_memory_quantity("2Gi") == 2 * 1024**3

    def test_decimal_megabytes(self):
        assert parse_memory_quantity("500M") == 500 * 10**6

    def test_plain_bytes(self):
        assert parse_memory_quantity("1048576") == 1048576

    def test_numeric_input(self):
        assert parse_memory_quantity(4096) == 4096

    def test_invalid(self):
        assert parse_memory_quantity("bad") is None

    def test_none(self):
        assert parse_memory_quantity(None) is None
