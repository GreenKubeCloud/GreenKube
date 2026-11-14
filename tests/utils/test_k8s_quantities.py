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


class TestFormatQuantities:
    """Quantities written to Git are canonical and human-readable (e2e: Helm values got ``24114500``)."""

    def test_cpu(self):
        from greenkube.utils.k8s_quantities import format_cpu_quantity

        assert format_cpu_quantity(250) == "250m"
        assert format_cpu_quantity(2000) == "2"
        assert format_cpu_quantity(1500) == "1500m"

    def test_memory_in_the_largest_exact_unit(self):
        from greenkube.utils.k8s_quantities import format_memory_quantity

        assert format_memory_quantity(256 * 1024**2) == "256Mi"
        assert format_memory_quantity(2 * 1024**3) == "2Gi"
        assert format_memory_quantity(1536 * 1024**2) == "1536Mi"

    def test_a_raw_byte_count_is_rounded_up_to_a_mebibyte(self):
        from greenkube.utils.k8s_quantities import format_memory_quantity, parse_memory_quantity

        assert format_memory_quantity(24114500) == "23Mi"
        parsed = parse_memory_quantity(format_memory_quantity(24114500))
        assert parsed is not None and parsed >= 24114500
        assert format_memory_quantity(23549 * 1024) == "23Mi"
        assert format_memory_quantity(1000) == "1Ki"

    def test_exact_keeps_the_byte_count(self):
        from greenkube.utils.k8s_quantities import format_memory_quantity

        assert format_memory_quantity(24114500, exact=True) == "24114500"
        assert format_memory_quantity(23549 * 1024, exact=True) == "23549Ki"


class TestQuantize:
    def test_memory_steps_are_about_five_percent_and_round_up(self):
        from greenkube.utils.k8s_quantities import quantize_memory_bytes

        mib = 1024**2
        assert quantize_memory_bytes(24114500) == 23 * mib
        assert quantize_memory_bytes(22 * mib + 4096) == 23 * mib
        assert quantize_memory_bytes(300 * mib - 5) == 304 * mib
        assert quantize_memory_bytes(3 * 1024 * mib) == 3 * 1024 * mib
        assert quantize_memory_bytes(3 * 1024 * mib + 4096) == 3200 * mib
        assert quantize_memory_bytes(1) == mib

    def test_byte_jitter_does_not_move_the_quantized_value(self):
        from greenkube.utils.k8s_quantities import quantize_memory_bytes

        values = {quantize_memory_bytes(301 * 1024**2 + jitter) for jitter in range(0, 4096, 97)}
        assert values == {304 * 1024**2}

    def test_cpu_steps(self):
        from greenkube.utils.k8s_quantities import quantize_cpu_millicores

        assert quantize_cpu_millicores(37) == 37
        assert quantize_cpu_millicores(101) == 105
        assert quantize_cpu_millicores(243.2) == 250
        assert quantize_cpu_millicores(1010) == 1050
        assert quantize_cpu_millicores(0.2) == 1
