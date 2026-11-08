"""Human-readable units for pull-request titles, bodies and comments (roadmap J1)."""

from __future__ import annotations

import math
from datetime import datetime, timezone

_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
_GIB = 1024**3
_MIB = 1024**2
_KIB = 1024


def inline(value: object, *, max_chars: int = 1_000) -> str:
    """One-line Markdown-safe text: collapsed whitespace and escaped backticks."""
    text = " ".join(str(value).split()).replace("`", "\\`")
    return text if len(text) <= max_chars else f"{text[:max_chars].rstrip()}..."


def table_cell(value: object, *, max_chars: int = 300) -> str:
    """One-line text safe inside a Markdown table cell (code spans are kept)."""
    text = " ".join(str(value).split()).replace("|", "\\|")
    return text if len(text) <= max_chars else f"{text[:max_chars].rstrip()}..."


def cpu(millicores: int | float | None) -> str:
    """Millicores below ten cores (``500m``, ``1000m``) so values compare at a glance; cores above (``12``)."""
    if millicores is None or not math.isfinite(millicores):
        return "n/a"
    value = round(millicores)
    if value >= 10_000 and value % 100 == 0:
        return f"{value / 1000:g}"
    return f"{value}m"


def memory(size_bytes: int | float | None) -> str:
    """``512Mi``, ``1Gi``, ``1.5Gi`` or ``n/a``."""
    if size_bytes is None or not math.isfinite(size_bytes):
        return "n/a"
    value = round(size_bytes)
    for suffix, factor in (("Gi", _GIB), ("Mi", _MIB), ("Ki", _KIB)):
        if value >= factor:
            if value % factor == 0:
                return f"{value // factor}{suffix}"
            if factor > _KIB and (value * 10) % factor == 0:
                return f"{value / factor:g}{suffix}"
    if value >= _MIB:
        return f"{value / _MIB:.0f}Mi"
    return f"{value}B"


def currency_symbol(currency: str | None) -> str:
    code = (currency or "USD").upper()
    return _CURRENCY_SYMBOLS.get(code, f"{code} ")


def money_compact(amount: float, currency: str | None = "USD") -> str:
    """Compact absolute amount for titles: ``$412``, ``$2.3k``, ``$1.2M``."""
    symbol = currency_symbol(currency)
    value = abs(amount)
    if value >= 1_000_000:
        text = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    elif value >= 10_000:
        text = f"{value / 1000:.0f}k"
    elif value >= 1_000:
        text = f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    elif value >= 10:
        text = str(math.floor(value + 0.5))
    else:
        text = f"{value:.2f}"
    return f"{symbol}{text}"


def money(amount: float | None, currency: str | None = "USD") -> str:
    """Body amount with sign for negatives: ``$412.50``, ``-$3.10``, ``n/a``."""
    if amount is None or not math.isfinite(amount):
        return "n/a"
    sign = "-" if amount < 0 else ""
    return f"{sign}{currency_symbol(currency)}{abs(amount):,.2f}"


def unit_price(amount: float | None, currency: str | None = "USD") -> str:
    """Hourly unit price with four decimals below one unit: ``$0.0316``, ``$0.0042``."""
    if amount is None or not math.isfinite(amount):
        return "n/a"
    return f"{currency_symbol(currency)}{amount:.4g}" if amount >= 1 else f"{currency_symbol(currency)}{amount:.4f}"


def co2e_compact(grams: float) -> str:
    """Compact CO2e for titles: ``350 gCO₂e``, ``18 kgCO₂e``, ``1.2 tCO₂e``."""
    value = abs(grams)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + " tCO₂e"
    if value >= 1_000:
        kilograms = value / 1000
        text = f"{kilograms:.0f}" if kilograms >= 10 else f"{kilograms:.1f}".rstrip("0").rstrip(".")
        return f"{text} kgCO₂e"
    return f"{value:.0f} gCO₂e"


def co2e(grams: float | None) -> str:
    """Body CO2e with the unit chosen for readability, or ``n/a``."""
    if grams is None or not math.isfinite(grams):
        return "n/a"
    sign = "-" if grams < 0 else ""
    value = abs(grams)
    if value >= 1_000_000:
        return f"{sign}{value / 1_000_000:,.2f} tCO₂e"
    if value >= 1_000:
        return f"{sign}{value / 1000:,.1f} kgCO₂e"
    return f"{sign}{value:,.0f} gCO₂e"


def percent(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.0%}"


def timestamp(value: datetime | None) -> str:
    """UTC timestamp to the minute, e.g. ``2026-11-08 14:05 UTC``."""
    if value is None:
        return "n/a"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def duration_hours(hours: float | None) -> str:
    """``24h``, ``3 days``, ``7 days``."""
    if hours is None or not math.isfinite(hours) or hours < 0:
        return "n/a"
    if hours >= 48 and hours % 24 == 0:
        return f"{hours / 24:g} days"
    if hours >= 48:
        return f"{hours / 24:.1f} days"
    return f"{hours:g}h"


def short_sha(value: str | None) -> str:
    return value[:12] if isinstance(value, str) and value else "n/a"
