# src/greenkube/utils/k8s_quantities.py
"""Parsers for Kubernetes resource quantity strings.

Implements the subset of the Kubernetes quantity grammar needed by the
recommendation connectors (VPA targets, lower/upper bounds):

- CPU quantities are returned as millicores (``"250m"`` → ``250``, ``"1"`` → ``1000``).
- Memory quantities are returned as bytes (``"512Mi"`` → ``536870912``).
"""

from typing import Optional

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
