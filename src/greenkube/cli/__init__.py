# src/greenkube/cli/__init__.py
"""
GreenKube CLI Package

This package exposes the top-level Typer `app` so tests and the console
entrypoint can import `greenkube.cli.app` as before the refactor.
"""

import logging

from ..core.processor import DataProcessor
from ..core.recommender import Recommender

# Re-export commonly patched symbols for tests
from ..reporters.console_reporter import ConsoleReporter
from .main import app

logger = logging.getLogger(__name__)

__all__ = ["app", "ConsoleReporter", "DataProcessor", "Recommender"]
