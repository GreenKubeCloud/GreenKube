# src/greenkube/utils/log.py
"""
Centralised logging configuration for GreenKube.

Configures structlog to emit structured JSON logs (for Loki / Grafana)
or human-readable console logs, depending on the ``LOG_FORMAT``
environment variable (``json`` | ``console``).

All existing ``logging.getLogger(__name__)`` call-sites keep working
unchanged – structlog transparently intercepts stdlib log records and
reformats them.  For rich per-call context (namespace, collector …),
callers can bind key-value pairs into the async-safe context store with::

    import structlog
    structlog.contextvars.bind_contextvars(namespace="kube-system", collector="prometheus")
    # … later in the same async task …
    structlog.contextvars.clear_contextvars()

Those fields are automatically merged into every log record emitted
during the lifetime of the bound context, making them first-class
labels for Loki LogQL queries.
"""

import logging
import sys
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Processors shared by both the structlog native chain and the stdlib
# "foreign" pre-chain (records that enter via logging.getLogger).
# ---------------------------------------------------------------------------
_SHARED_PRE_PROCESSORS: list[Any] = [
    # Merge context-vars (namespace, collector, …) into every event.
    structlog.contextvars.merge_contextvars,
    # Add the stdlib log level as a "level" key.
    structlog.stdlib.add_log_level,
    # Add the logger name (module) as a "logger" key.
    structlog.stdlib.add_logger_name,
    # Expand printf-style positional args: logger.info("x=%s", 1) → "x=1".
    structlog.stdlib.PositionalArgumentsFormatter(),
    # ISO-8601 UTC timestamp.
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    # Render nested stack-info frames.
    structlog.processors.StackInfoRenderer(),
]


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Set up structlog + stdlib logging.

    Must be called once at application startup (CLI entry-point or API
    ``main()``).  Subsequent calls are idempotent: the root logger is
    cleared and reconfigured.

    Args:
        level:      Minimum log level string (``DEBUG``, ``INFO``, …).
        log_format: ``"json"`` for Loki-ready JSON output;
                    ``"console"`` for human-readable coloured output.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    use_json = log_format.lower() != "console"

    # ------------------------------------------------------------------
    # Renderer — last step: either JSON or pretty console.
    # ------------------------------------------------------------------
    if use_json:
        final_renderer: Any = structlog.processors.JSONRenderer()
    else:
        final_renderer = structlog.dev.ConsoleRenderer(colors=True)

    # ------------------------------------------------------------------
    # structlog native chain (for callers that import structlog directly).
    # ------------------------------------------------------------------
    structlog.configure(
        processors=[
            *_SHARED_PRE_PROCESSORS,
            # Bridge to the stdlib ProcessorFormatter below.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ------------------------------------------------------------------
    # stdlib formatter — used for ALL handlers (native + foreign records).
    # ------------------------------------------------------------------
    formatter = structlog.stdlib.ProcessorFormatter(
        # Pre-chain applied to stdlib records that did NOT go through structlog.
        foreign_pre_chain=_SHARED_PRE_PROCESSORS,
        # Final processors applied to every record.
        processors=[
            # Drop the internal structlog metadata wrapper.
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            final_renderer,
        ],
    )

    # ------------------------------------------------------------------
    # Root handler — stdout for container-friendly log shipping.
    # ------------------------------------------------------------------
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # ------------------------------------------------------------------
    # Quiet noisy third-party libraries so they don't flood Loki.
    # ------------------------------------------------------------------
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("kubernetes_asyncio").setLevel(logging.WARNING)
