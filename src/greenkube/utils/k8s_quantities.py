# src/greenkube/utils/k8s_quantities.py
"""Parsers, formatters and quantization of Kubernetes resource quantities.

Implements the subset of the Kubernetes quantity grammar needed by the
recommendation connectors (VPA targets, lower/upper bounds):

- CPU quantities are returned as millicores (``"250m"`` → ``250``, ``"1"`` → ``1000``).
- Memory quantities are returned as bytes (``"512Mi"`` → ``536870912``).

Every quantity GreenKube writes to Git goes through :func:`format_cpu_quantity`
and :func:`format_memory_quantity`: canonical, human units (``250m``, ``2``,
``23Mi``, ``1Gi``), never a raw byte count such as ``24114500``.
"""

import math
from typing import Optional

MIB = 1024**2
#: Binary units of formatted memory quantities, largest first.
_MEMORY_UNITS = (
    ("Ei", 1024**6),
    ("Pi", 1024**5),
    ("Ti", 1024**4),
    ("Gi", 1024**3),
    ("Mi", 1024**2),
    ("Ki", 1024),
)

_BINARY_SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}

_DECIMAL_SUFFIXES = {
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
}


def _parse_scaled(value: object) -> Optional[float]:
    """Parses a quantity string into its base-unit float value, or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    for suffix, multiplier in _BINARY_SUFFIXES.items():
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * multiplier
            except ValueError:
                return None

    if text[-1] in _DECIMAL_SUFFIXES:
        try:
            return float(text[:-1]) * _DECIMAL_SUFFIXES[text[-1]]
        except ValueError:
            return None

    try:
        return float(text)
    except ValueError:
        return None


def parse_cpu_quantity(value: object) -> Optional[int]:
    """Parses a Kubernetes CPU quantity into millicores."""
    cores = _parse_scaled(value)
    if cores is None:
        return None
    return int(round(cores * 1000))


def parse_memory_quantity(value: object) -> Optional[int]:
    """Parses a Kubernetes memory quantity into bytes."""
    parsed = _parse_scaled(value)
    if parsed is None:
        return None
    return int(round(parsed))


def format_cpu_quantity(millicores: int) -> str:
    """A positive millicore value as a canonical CPU quantity: ``2`` for whole cores, else ``250m``."""
    value = int(millicores)
    cores, remainder = divmod(value, 1000)
    return str(cores) if remainder == 0 else f"{value}m"


def format_memory_quantity(byte_count: int, *, exact: bool = False) -> str:
    """A positive byte value in its largest exact binary unit (``23Mi``, ``1Gi``).

    A value that is not a whole number of mebibytes (``24114500``) is rounded
    **up** to the next mebibyte (``23Mi``), or kibibyte below one mebibyte, so
    Git never receives a raw byte count and the written request is never below
    the proposal. ``exact`` keeps the value unchanged (``24114500`` stays a
    byte count): rollbacks restore the pre-apply quantity byte for byte.
    """
    value = int(byte_count)
    unit_floor = MIB if value >= MIB else 1024
    if not exact and value > 0 and value % unit_floor:
        value = int(math.ceil(value / unit_floor)) * unit_floor
    for suffix, factor in _MEMORY_UNITS:
        if value and value % factor == 0:
            return f"{value // factor}{suffix}"
    return str(value)
