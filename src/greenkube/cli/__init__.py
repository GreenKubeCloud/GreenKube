# src/greenkube/cli/__init__.py
"""
GreenKube CLI Package

This package exposes the top-level Typer `app` so the console
entrypoint can import `greenkube.cli.app`.
"""

from .main import app

__all__ = ["app"]
