# tests/utils/test_log.py
"""Tests for the structured-logging configuration module."""

import json
import logging

import structlog

from greenkube.utils.log import configure_logging


class TestConfigureLogging:
    """Tests for :func:`configure_logging`."""

    def _capture_json_record(self, capfd, level: str = "INFO") -> dict:
        """Configure JSON logging, emit one record, and return the parsed JSON."""
        configure_logging(level=level, log_format="json")
        logger = logging.getLogger("test.json_output")
        logger.info("hello structured world")
        captured = capfd.readouterr()
        return json.loads(captured.out.strip())

    def test_json_output_is_valid_json(self, capfd):
        """JSON format must produce parseable JSON on stdout."""
        record = self._capture_json_record(capfd)
        assert isinstance(record, dict)

    def test_json_contains_event(self, capfd):
        """The log message must be in the 'event' key."""
        record = self._capture_json_record(capfd)
        assert record.get("event") == "hello structured world"

    def test_json_contains_level(self, capfd):
        """JSON record must include a 'level' key."""
        record = self._capture_json_record(capfd)
        assert "level" in record
        assert record["level"] == "info"

    def test_json_contains_logger(self, capfd):
        """JSON record must include the logger name."""
        record = self._capture_json_record(capfd)
        assert record.get("logger") == "test.json_output"

    def test_json_contains_timestamp(self, capfd):
        """JSON record must include an ISO-8601 timestamp."""
        record = self._capture_json_record(capfd)
        assert "timestamp" in record
        assert "T" in record["timestamp"]  # basic ISO-8601 check

    def test_log_level_filtering(self, capfd):
        """Records below the configured level must be suppressed."""
        configure_logging(level="WARNING", log_format="json")
        logger = logging.getLogger("test.filtering")
        logger.debug("should be suppressed")
        logger.info("also suppressed")
        logger.warning("this appears")
        out = capfd.readouterr().out
        records = [json.loads(line) for line in out.strip().splitlines() if line.strip()]
        assert len(records) == 1
        assert records[0]["event"] == "this appears"

    def test_positional_args_expanded(self, capfd):
        """Printf-style positional args must be expanded into the event string."""
        configure_logging(level="INFO", log_format="json")
        logger = logging.getLogger("test.positional")
        logger.info("value is %s and %d", "hello", 42)
        out = capfd.readouterr().out
        record = json.loads(out.strip())
        assert record["event"] == "value is hello and 42"

    def test_console_format_does_not_crash(self, capfd):
        """Console format must not raise and must produce non-empty output."""
        configure_logging(level="INFO", log_format="console")
        logger = logging.getLogger("test.console")
        logger.info("console test message")
        out = capfd.readouterr().out
        assert "console test message" in out

    def test_contextvars_merged_into_json(self, capfd):
        """Context variables (namespace, collector) must appear in JSON records."""
        configure_logging(level="INFO", log_format="json")
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(namespace="kube-system", collector="prometheus")
        try:
            logger = logging.getLogger("test.contextvars")
            logger.info("ctx test")
            out = capfd.readouterr().out
            record = json.loads(out.strip())
            assert record.get("namespace") == "kube-system"
            assert record.get("collector") == "prometheus"
        finally:
            structlog.contextvars.clear_contextvars()

    def test_repeated_configure_is_idempotent(self, capfd):
        """Calling configure_logging twice must not duplicate handlers."""
        configure_logging(level="INFO", log_format="json")
        configure_logging(level="INFO", log_format="json")
        logger = logging.getLogger("test.idempotent")
        logger.info("once")
        out = capfd.readouterr().out
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        assert len(lines) == 1, "Message must appear exactly once despite double configure"
