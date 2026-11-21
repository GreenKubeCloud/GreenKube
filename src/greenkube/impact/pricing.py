# src/greenkube/impact/pricing.py
"""Unit prices for the Impact Model: the cost fallback chain (roadmap B3).

Prices are resolved per node by :class:`UnitPriceResolver`, first tier wins:

1. **Observed** per-resource allocated cost (``cost_cpu`` / ``cost_ram`` on
   metric rows, written by OpenCost/Kubecost collection), divided by the
   reserved core-hours and GiB-hours of the same rows.
2. **PricingCatalog**: bundled on-demand list price of the node's instance type
   in its region, apportioned to CPU and RAM with the OpenCost ratio. Only
   catalogs in the configured currency are used: GreenKube never converts
   currencies, so a EUR catalog does not price a USD cluster.
3. **CustomRates**: per core-hour / GiB-hour rates from configuration, or the
   OpenCost default custom pricing when none are configured.

No price in this module is invented: defaults come from OpenCost's published
``configs/default.json`` (see ``data/pricing/opencost_default.json``) and the
catalog files are generated from public price lists by
``scripts/generate_pricing_catalog.py``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from greenkube.impact.types import BYTES_PER_GIB, CostSource, UnitPrices, parse_cost_source

logger = logging.getLogger(__name__)

PRICING_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "pricing"

#: Catalog files bundled with GreenKube, keyed by ``NodeInfo.cloud_provider``.
CATALOG_PROVIDERS = ("aws", "gcp", "azure", "ovh", "scaleway")

#: Supported catalog schema version.
CATALOG_SCHEMA_VERSION = 1

#: Catalog region key for prices that apply to every region of a provider.
ANY_REGION = "*"

#: Provider aliases seen in node metadata.
_PROVIDER_ALIASES = {"google": "gcp", "gke": "gcp", "eks": "aws", "aks": "azure", "ovhcloud": "ovh"}


@dataclass(frozen=True)
class DefaultRates:
    """OpenCost default custom pricing (hourly rates)."""

    cpu_core_hour: float
    ram_gib_hour: float
    storage_gib_hour: float
    currency: str
    source: str


@lru_cache(maxsize=1)
def opencost_default_rates() -> DefaultRates:
    """Loads the OpenCost default custom pricing bundled with GreenKube."""
    with open(PRICING_DATA_DIR / "opencost_default.json", encoding="utf-8") as handle:
        data = json.load(handle)
    prices = data["prices"]
    return DefaultRates(
        cpu_core_hour=float(prices["CPU"]),
        ram_gib_hour=float(prices["RAM"]),
        storage_gib_hour=float(prices["storage"]),
        currency=str(data.get("currency", "USD")),
        source=str(data.get("source", "")),
    )


def apportion_node_price(
    node_hourly_price: float,
    vcpus: float,
    ram_gib: float,
    cpu_to_ram_ratio: Optional[float] = None,
) -> Tuple[float, float]:
    """Splits a node's hourly price into per core-hour and per GiB-hour prices.

    This is OpenCost's algorithm for nodes whose list price has no CPU/RAM
    breakdown (``pkg/costmodel/costmodel.go``)::

        ram_price = node_price / (vcpus × cpu_to_ram_ratio + ram_gib)
        cpu_price = ram_price × cpu_to_ram_ratio

    with ``cpu_to_ram_ratio = default CPU price / default RAM price``.
    """
    if node_hourly_price <= 0:
        return 0.0, 0.0
    if cpu_to_ram_ratio is None:
        defaults = opencost_default_rates()
        cpu_to_ram_ratio = defaults.cpu_core_hour / defaults.ram_gib_hour
    vcpus = max(float(vcpus), 0.0)
    ram_gib = max(float(ram_gib), 0.0)
    ram_multiple = vcpus * cpu_to_ram_ratio + ram_gib
    if ram_multiple <= 0:
        return 0.0, 0.0
    ram_price = node_hourly_price / ram_multiple
    return ram_price * cpu_to_ram_ratio, ram_price


@dataclass(frozen=True)
class CatalogEntry:
    """List price of one instance type in one region."""

    provider: str
    region: str
    instance_type: str
    hourly: float
    vcpus: Optional[float]
    memory_gib: Optional[float]
    currency: str


def normalize_provider(provider: Optional[str]) -> Optional[str]:
    if not provider:
        return None
    value = provider.strip().lower()
    return _PROVIDER_ALIASES.get(value, value)


class PricingCatalog:
    """Bundled on-demand list prices per provider, region and instance type.

    Files live in ``data/pricing/<provider>.json`` and follow this schema::

        {"schema_version": 1, "provider": "aws", "currency": "USD",
         "columns": ["hourly", "vcpu", "memory_gib"],
         "regions": {"eu-west-3": {"m5.large": [0.112, 2, 8.0]}}}

    The ``"*"`` region holds prices valid in every region of the provider and
    is used when no region-specific entry exists. A provider file with an empty
    ``regions`` map is valid: the resolver then falls through to custom rates.
    """

    def __init__(self, data_dir: Path = PRICING_DATA_DIR, documents: Optional[Mapping[str, Mapping]] = None):
        self._data_dir = data_dir
        self._documents: Dict[str, Optional[Mapping]] = dict(documents or {})
        self._indexes: Dict[str, Dict[Tuple[str, str], CatalogEntry]] = {}

    def _document(self, provider: str) -> Optional[Mapping]:
        if provider not in self._documents:
            path = self._data_dir / f"{provider}.json"
            try:
                with open(path, encoding="utf-8") as handle:
                    self._documents[provider] = json.load(handle)
            except FileNotFoundError:
                self._documents[provider] = None
            except (OSError, ValueError) as exc:
                logger.warning("Could not load pricing catalog %s: %s", path, exc)
                self._documents[provider] = None
        return self._documents[provider]

    def _index(self, provider: str) -> Dict[Tuple[str, str], CatalogEntry]:
        if provider in self._indexes:
            return self._indexes[provider]
        index: Dict[Tuple[str, str], CatalogEntry] = {}
        document = self._document(provider)
        if document and int(document.get("schema_version", 0)) == CATALOG_SCHEMA_VERSION:
            currency = str(document.get("currency", "USD"))
            columns = list(document.get("columns", ["hourly", "vcpu", "memory_gib"]))
            for region, instances in (document.get("regions") or {}).items():
                for instance_type, row in (instances or {}).items():
                    entry = _catalog_entry(provider, region, instance_type, row, columns, currency)
                    if entry is not None:
                        index[(region.lower(), instance_type.lower())] = entry
        self._indexes[provider] = index
        return index

    def lookup(
        self,
        provider: Optional[str],
        region: Optional[str],
        instance_type: Optional[str],
    ) -> Optional[CatalogEntry]:
        """Returns the list price of an instance type in a region, if bundled."""
        provider_key = normalize_provider(provider)
        if not provider_key or provider_key not in CATALOG_PROVIDERS or not instance_type:
            return None
        index = self._index(provider_key)
        instance_key = instance_type.strip().lower()
        if region:
            entry = index.get((region.strip().lower(), instance_key))
            if entry is not None:
                return entry
        # "*" holds prices that apply to every region of a provider (e.g. OVHcloud).
        return index.get((ANY_REGION, instance_key))

    def size(self, provider: str) -> int:
        """Number of (region, instance type) entries bundled for a provider."""
        return len(self._index(provider))


def _catalog_entry(
    provider: str,
    region: str,
    instance_type: str,
    row: Any,
    columns: list,
    currency: str,
) -> Optional[CatalogEntry]:
    if isinstance(row, Mapping):
        values = row
    elif isinstance(row, (list, tuple)):
        values = dict(zip(columns, row))
    else:
        return None
    try:
        hourly = float(values["hourly"])
    except (KeyError, TypeError, ValueError):
        return None
    if hourly <= 0:
        return None
    return CatalogEntry(
        provider=provider,
        region=region,
        instance_type=instance_type,
        hourly=hourly,
        vcpus=_optional_float(values.get("vcpu")),
        memory_gib=_optional_float(values.get("memory_gib")),
        currency=currency,
    )


def _optional_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass(frozen=True)
class CustomRates:
    """Per core-hour and per GiB-hour rates from configuration.

    ``configured`` is False when the OpenCost defaults are in use.
    """

    cpu_core_hour: float
    ram_gib_hour: float
    currency: str
    configured: bool

    @classmethod
    def from_config(cls, config: Any) -> "CustomRates":
        defaults = opencost_default_rates()
        cpu = _config_float(config, "IMPACT_PRICING_CPU_CORE_HOUR")
        ram = _config_float(config, "IMPACT_PRICING_RAM_GIB_HOUR")
        currency = getattr(config, "IMPACT_PRICING_CURRENCY", None)
        currency = currency if isinstance(currency, str) and currency.strip() else defaults.currency
        configured = cpu is not None or ram is not None
        return cls(
            cpu_core_hour=cpu if cpu is not None else defaults.cpu_core_hour,
            ram_gib_hour=ram if ram is not None else defaults.ram_gib_hour,
            currency=currency.strip().upper(),
            configured=configured,
        )

    def unit_prices(self) -> UnitPrices:
        return UnitPrices(
            cpu_core_hour=self.cpu_core_hour,
            ram_gib_hour=self.ram_gib_hour,
            currency=self.currency,
            source=CostSource.CUSTOM_RATE,
            detail="configured" if self.configured else "opencost_default",
        )


def _config_float(config: Any, name: str) -> Optional[float]:
    value = getattr(config, name, None)
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


@dataclass
class ObservedRates:
    """Accumulates observed per-resource cost and reserved quantities for one node."""

    cpu_cost: float = 0.0
    cpu_core_hours: float = 0.0
    ram_cost: float = 0.0
    ram_gib_hours: float = 0.0
    sources: Dict[str, int] = field(default_factory=dict)

    def add(
        self,
        *,
        cost_cpu: Optional[float],
        cost_ram: Optional[float],
        cpu_reserved_cores: float,
        mem_reserved_gib: float,
        hours: float,
        source: Optional[str],
    ) -> None:
        if hours <= 0:
            return
        if cost_cpu is not None and cost_cpu >= 0 and cpu_reserved_cores > 0:
            self.cpu_cost += cost_cpu
            self.cpu_core_hours += cpu_reserved_cores * hours
        if cost_ram is not None and cost_ram >= 0 and mem_reserved_gib > 0:
            self.ram_cost += cost_ram
            self.ram_gib_hours += mem_reserved_gib * hours
        key = source or CostSource.OPENCOST.value
        self.sources[key] = self.sources.get(key, 0) + 1

    def unit_prices(self, currency: str) -> Optional[UnitPrices]:
        """Both resources must be observed with positive prices, otherwise fall through."""
        if self.cpu_core_hours <= 0 or self.ram_gib_hours <= 0 or self.cpu_cost <= 0 or self.ram_cost <= 0:
            return None
        source_name = max(self.sources.items(), key=lambda item: item[1])[0] if self.sources else None
        source, unknown_label = parse_cost_source(source_name)
        return UnitPrices(
            cpu_core_hour=self.cpu_cost / self.cpu_core_hours,
            ram_gib_hour=self.ram_cost / self.ram_gib_hours,
            currency=currency,
            source=source,
            detail=f"observed:{unknown_label}" if unknown_label else "observed",
        )


class UnitPriceResolver:
    """Resolves per-node unit prices through the observed → catalog → custom chain."""

    def __init__(
        self,
        catalog: Optional[PricingCatalog] = None,
        custom_rates: Optional[CustomRates] = None,
        observed: Optional[Mapping[str, ObservedRates]] = None,
    ):
        self.catalog = catalog if catalog is not None else PricingCatalog()
        self.custom_rates = custom_rates if custom_rates is not None else CustomRates.from_config(None)
        self.observed: Dict[str, ObservedRates] = dict(observed or {})

    def resolve(
        self,
        *,
        node_name: Optional[str],
        provider: Optional[str] = None,
        region: Optional[str] = None,
        instance_type: Optional[str] = None,
        vcpus: Optional[float] = None,
        memory_gib: Optional[float] = None,
    ) -> UnitPrices:
        """Returns the unit prices of a node, recording the tier that produced them."""
        if node_name and node_name in self.observed:
            observed = self.observed[node_name].unit_prices(self.custom_rates.currency)
            if observed is not None:
                return observed

        entry = self.catalog.lookup(provider, region, instance_type)
        if entry is not None and entry.currency.upper() != self.custom_rates.currency:
            logger.debug(
                "Skipping %s catalog price for %s: currency %s differs from IMPACT_PRICING_CURRENCY %s",
                entry.provider,
                instance_type,
                entry.currency,
                self.custom_rates.currency,
            )
            entry = None
        if entry is not None:
            entry_vcpus = entry.vcpus or vcpus
            entry_memory = entry.memory_gib or memory_gib
            if entry_vcpus and entry_memory:
                cpu_price, ram_price = apportion_node_price(entry.hourly, entry_vcpus, entry_memory)
                if cpu_price > 0 and ram_price > 0:
                    return UnitPrices(
                        cpu_core_hour=cpu_price,
                        ram_gib_hour=ram_price,
                        currency=entry.currency,
                        source=CostSource.PRICING_CATALOG,
                        detail=f"{entry.provider}/{entry.region}/{entry.instance_type}",
                    )

        return self.custom_rates.unit_prices()


def cost_split_share(row: Any) -> Optional[float]:
    """Share of an aggregated row's duration whose cost carries a CPU/RAM split (F-03).

    None when the row does not say (raw rows, or no duration): its split, when
    present, covers the whole row.
    """
    split_seconds = getattr(row, "cost_split_duration_seconds", None)
    duration = getattr(row, "duration_seconds", None)
    if not isinstance(split_seconds, (int, float)) or isinstance(split_seconds, bool):
        return None
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
        return None
    return min(max(float(split_seconds) / float(duration), 0.0), 1.0)


def cost_split_hours(row: Any, hours: float) -> float:
    """Hours of a row covered by its CPU/RAM cost split: all of ``hours`` unless the row says otherwise."""
    share = cost_split_share(row)
    return hours if share is None else hours * share


def observed_rates_from_rows(rows: Iterable[Any], default_hours: float) -> Dict[str, ObservedRates]:
    """Builds per-node observed rates from metric rows carrying ``cost_cpu``/``cost_ram``.

    Rows without these optional fields (every row before roadmap A4/A11) are
    skipped, so the resolver falls through to the catalog and custom rates.
    An aggregated row whose split covers only part of its hours (hours
    re-aggregated from 0.3.x rows carry a total only, F-03) counts the
    reserved quantities of the split hours only.
    """
    observed: Dict[str, ObservedRates] = {}
    for row in rows:
        cost_cpu = getattr(row, "cost_cpu", None)
        cost_ram = getattr(row, "cost_ram", None)
        node = getattr(row, "node", None)
        if node is None or (cost_cpu is None and cost_ram is None):
            continue
        hours = cost_split_hours(row, row_hours(row, default_hours))
        if hours <= 0:
            continue
        cpu_reserved = max(float(getattr(row, "cpu_request", 0) or 0), float(row.cpu_usage_millicores or 0)) / 1000.0
        mem_reserved = (
            max(float(getattr(row, "memory_request", 0) or 0), float(row.memory_usage_bytes or 0)) / BYTES_PER_GIB
        )
        observed.setdefault(node, ObservedRates()).add(
            cost_cpu=_optional_number(cost_cpu),
            cost_ram=_optional_number(cost_ram),
            cpu_reserved_cores=cpu_reserved,
            mem_reserved_gib=mem_reserved,
            hours=hours,
            source=getattr(row, "cost_source", None),
        )
    return observed


def row_hours(row: Any, default_hours: float) -> float:
    """Duration a metric row covers, in hours (``duration_seconds`` or the scrape step)."""
    duration = getattr(row, "duration_seconds", None)
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
        return float(duration) / 3600.0
    samples = getattr(row, "sample_count", 1)
    samples = samples if isinstance(samples, int) and samples > 0 else 1
    return default_hours * samples


def _optional_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
