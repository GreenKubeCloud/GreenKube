from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "pg_upgrade_17_to_18.sh"


def test_upgrade_script_is_non_interactive_and_hardened():
    text = SCRIPT.read_text()

    assert "set -euo pipefail" in text
    assert "BACKUP_COMMAND:-" in text
    assert "BACKUP_VERIFY_COMMAND:-" in text
    assert "POSTGRES_ROLE" in text
    assert "ACTION=rollback" in text
    assert "pgdata_pg17_bak" in text
    assert "read " not in text
    assert "kubectl exec" not in text


def test_upgrade_script_does_not_echo_secret_or_connection_string():
    text = SCRIPT.read_text()

    assert "echo $POSTGRES_PASSWORD" not in text
    assert "Connection:" not in text
    assert "set -x" not in text
    assert "POSTGRES_PASSWORD" in text
