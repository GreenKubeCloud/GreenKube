from greenkube.utils.log import log_safe


def test_log_safe_escapes_line_breaks():
    assert log_safe("alice\r\nINFO forged entry") == "alice\\r\\nINFO forged entry"


def test_log_safe_keeps_plain_values():
    assert log_safe("prod-cluster") == "prod-cluster"
    assert log_safe(None) == "None"
