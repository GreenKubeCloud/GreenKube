#!/usr/bin/env python3
"""Generate GreenKube's bundled PricingCatalog from public cloud price lists.

The Impact Model (``src/greenkube/impact/pricing.py``) reads on-demand hourly
list prices per provider, region and instance type from
``src/greenkube/data/pricing/<provider>.json``. This script regenerates those
files from public, unauthenticated price lists; GreenKube never scrapes prices
at runtime.

Sources:

* ``aws``: AWS Price List Bulk API, one CSV offer file per region
  (https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/<region>/index.csv).
  On-demand, shared tenancy, Linux, no pre-installed software, ``RunInstances``.
  Each region file is ~150-200 MB, so restrict ``--regions`` when iterating.
* ``azure``: Azure Retail Prices API (https://prices.azure.com/api/retail/prices),
  ``Virtual Machines`` consumption prices, Linux only (no Windows, Spot or Low
  Priority meters). The API does not expose vCPU or memory sizes.
* ``ovh``: OVHcloud public order catalog
  (https://eu.api.ovh.com/v1/order/catalog/public/cloud?ovhSubsidiary=FR),
  hourly ``consumption`` plans. Standard regions share one price, stored under
  the ``"*"`` region.
* ``scaleway``: Scaleway public product catalog
  (https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products),
  instance products, keyed by region (zone without its ``-N`` suffix).
* ``gcp``: not generated. GCP machine prices are published per vCPU and per GiB
  through the Cloud Billing Catalog API, which requires an API key. GreenKube's
  default rates are OpenCost's, which are themselves GCP ``us-central1`` prices.

For the two large catalogs (AWS, Azure) only instance types with a GreenKube
power profile (``data/instance_power_profiles.csv``) are kept by default, so the
bundled files stay small and cover the instance types the energy model knows.
Use ``--all-instance-types`` to keep every type. OVHcloud and Scaleway catalogs
are small and always kept whole.

Usage::

    python scripts/generate_pricing_catalog.py --provider ovh --provider scaleway
    python scripts/generate_pricing_catalog.py --provider aws --regions eu-west-3,us-east-1
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "src" / "greenkube" / "data"
OUTPUT_DIR = DATA_DIR / "pricing"
USER_AGENT = "GreenKube-pricing-catalog-generator (+https://github.com/GreenKubeCloud/GreenKube)"
TIMEOUT_SECONDS = 120

AWS_REGION_CSV = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/{region}/index.csv"
AZURE_API = "https://prices.azure.com/api/retail/prices"
OVH_CATALOG = "https://eu.api.ovh.com/v1/order/catalog/public/cloud?ovhSubsidiary=FR"
SCALEWAY_CATALOG = "https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products"

#: Provider names used by the region mapping and power profile CSVs.
_MAPPING_PROVIDER = {"aws": "Amazon Web Services", "azure": "Microsoft Azure", "gcp": "Google Cloud Platform"}
_PROFILE_PROVIDER = {"aws": "AWS", "azure": "Azure", "gcp": "Google Cloud", "ovh": "OVH", "scaleway": "Scaleway"}

#: Providers whose catalogs are filtered to instance types with a power profile.
FILTERED_PROVIDERS = ("aws", "azure")

#: (hourly price, vCPUs or None, memory GiB or None)
Row = Tuple[float, Optional[float], Optional[float]]
Prices = Dict[str, Dict[str, Row]]


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                 #
# --------------------------------------------------------------------------- #


def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)  # noqa: S310 - fixed public HTTPS sources


def _get_json(url: str) -> dict:
    with _open(url) as response:
        return json.load(response)


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Reference data                                                               #
# --------------------------------------------------------------------------- #


def known_instance_types(provider: str) -> set[str]:
    """Instance types with a GreenKube power profile, lower-cased."""
    name = _PROFILE_PROVIDER[provider]
    with open(DATA_DIR / "instance_power_profiles.csv", encoding="utf-8") as handle:
        return {row["instance_type"].lower() for row in csv.DictReader(handle) if row["provider"] == name}


def mapped_regions(provider: str) -> List[str]:
    """Regions GreenKube maps to an Electricity Maps zone, for a provider."""
    name = _MAPPING_PROVIDER.get(provider)
    if name is None:
        return []
    with open(DATA_DIR / "cloud_region_electricity_maps_mapping.csv", encoding="utf-8") as handle:
        return sorted({row["region_id"] for row in csv.DictReader(handle) if row["cloud_provider"] == name})


def _number(value: object) -> Optional[float]:
    try:
        number = float(str(value).replace(",", "").split()[0])
    except (TypeError, ValueError, IndexError):
        return None
    return number if number > 0 else None


def _keep(prices: Prices, region: str, instance_type: str, row: Row, choose: Callable[[Row, Row], Row]) -> None:
    current = prices.setdefault(region, {}).get(instance_type)
    prices[region][instance_type] = row if current is None else choose(current, row)


def _lowest(first: Row, second: Row) -> Row:
    return first if first[0] <= second[0] else second


# --------------------------------------------------------------------------- #
# Parsers (pure, unit-tested with fixtures)                                    #
# --------------------------------------------------------------------------- #


def parse_aws_csv(lines: Iterable[str], keep: Optional[set[str]]) -> Prices:
    """Parses one AWS regional EC2 offer CSV (5 metadata lines, then a header)."""
    iterator: Iterator[str] = iter(lines)
    for _ in range(5):
        next(iterator, None)
    reader = csv.DictReader(iterator)
    prices: Prices = {}
    for record in reader:
        if (
            record.get("TermType") != "OnDemand"
            or record.get("Product Family") != "Compute Instance"
            or record.get("Tenancy") != "Shared"
            or record.get("Operating System") != "Linux"
            or record.get("Pre Installed S/W") != "NA"
            or record.get("CapacityStatus") != "Used"
            or record.get("operation") != "RunInstances"
            or record.get("Unit") not in ("Hrs", "Hours")
            or record.get("Currency") != "USD"
        ):
            continue
        instance_type = (record.get("Instance Type") or "").strip()
        region = (record.get("Region Code") or "").strip().lower()
        hourly = _number(record.get("PricePerUnit"))
        if not instance_type or not region or hourly is None:
            continue
        if keep is not None and instance_type.lower() not in keep:
            continue
        row: Row = (hourly, _number(record.get("vCPU")), _number(record.get("Memory")))
        _keep(prices, region, instance_type, row, _lowest)
    return prices


def parse_azure_items(items: Iterable[dict], keep: Optional[set[str]]) -> Prices:
    """Parses Azure Retail Prices API items (Linux, consumption, no Spot/Low Priority)."""
    prices: Prices = {}
    for item in items:
        product = item.get("productName") or ""
        sku_name = item.get("skuName") or ""
        if (
            item.get("type") != "Consumption"
            or item.get("serviceName") != "Virtual Machines"
            or item.get("unitOfMeasure") != "1 Hour"
            or "Windows" in product
            or "Spot" in sku_name
            or "Low Priority" in sku_name
            or item.get("currencyCode") != "USD"
        ):
            continue
        instance_type = (item.get("armSkuName") or "").strip()
        region = (item.get("armRegionName") or "").strip().lower()
        hourly = _number(item.get("retailPrice"))
        if not instance_type or not region or hourly is None:
            continue
        if keep is not None and instance_type.lower() not in keep:
            continue
        _keep(prices, region, instance_type, (hourly, None, None), _lowest)
    return prices


def parse_ovh_catalog(catalog: dict, keep: Optional[set[str]]) -> Tuple[Prices, str]:
    """Parses the OVHcloud public catalog: hourly ``<flavor>.consumption`` addons."""
    prices: Prices = {}
    for addon in catalog.get("addons", []):
        plan_code = addon.get("planCode") or ""
        if not plan_code.endswith(".consumption"):
            continue  # skips monthly plans and Local Zone variants (".consumption.LZ.*")
        flavor = plan_code[: -len(".consumption")]
        if keep is not None and flavor.lower() not in keep:
            continue
        hourly_pricing = [p for p in addon.get("pricings", []) if p.get("intervalUnit") == "hour"]
        if not hourly_pricing:
            continue
        hourly = _number(hourly_pricing[0].get("price"))
        if hourly is None:
            continue
        technical = ((addon.get("blobs") or {}).get("technical")) or {}
        vcpus = _number((technical.get("cpu") or {}).get("cores"))
        memory = _number((technical.get("memory") or {}).get("size"))
        # Catalog prices are expressed in 1e-8 of the currency unit.
        _keep(prices, "*", flavor, (hourly / 1e8, vcpus, memory), _lowest)
    currency = ((catalog.get("locale") or {}).get("currencyCode")) or "EUR"
    return prices, currency


def parse_scaleway_products(products: Iterable[dict], keep: Optional[set[str]]) -> Tuple[Prices, str]:
    """Parses Scaleway public catalog instance products, keyed by region."""
    prices: Prices = {}
    currency = "EUR"
    candidates: Dict[Tuple[str, str], Tuple[str, Row]] = {}
    for product in products:
        if product.get("product_category") != "Instance" or product.get("status") == "retired":
            continue
        if (product.get("unit_of_measure") or {}).get("unit") != "hour":
            continue
        name = (product.get("product") or "").strip()
        zone = ((product.get("locality") or {}).get("zone") or "").strip().lower()
        retail = (product.get("price") or {}).get("retail_price") or {}
        hourly = float(retail.get("units") or 0) + float(retail.get("nanos") or 0) / 1e9
        if not name or not zone or hourly <= 0:
            continue
        if keep is not None and name.lower() not in keep:
            continue
        currency = retail.get("currency_code") or currency
        hardware = (product.get("properties") or {}).get("hardware") or {}
        vcpus = _number(((hardware.get("cpu") or {}).get("virtual") or {}).get("count"))
        ram_bytes = _number((hardware.get("ram") or {}).get("size"))
        region = zone.rsplit("-", 1)[0] if zone[-1:].isdigit() else zone
        row: Row = (hourly, vcpus, ram_bytes / 1024**3 if ram_bytes else None)
        key = (region, name)
        # Deterministic: keep the lowest-numbered zone of each region.
        if key not in candidates or zone < candidates[key][0]:
            candidates[key] = (zone, row)
    for (region, name), (_zone, row) in candidates.items():
        prices.setdefault(region, {})[name] = row
    return prices, currency


# --------------------------------------------------------------------------- #
# Fetchers                                                                     #
# --------------------------------------------------------------------------- #


def fetch_aws(regions: Sequence[str], keep: Optional[set[str]]) -> Prices:
    prices: Prices = {}
    for region in regions:
        url = AWS_REGION_CSV.format(region=region)
        _log(f"aws: downloading {url}")
        try:
            with _open(url) as response:
                region_prices = parse_aws_csv(io.TextIOWrapper(response, encoding="utf-8"), keep)
        except Exception as exc:  # noqa: BLE001 - a missing region must not abort the run
            _log(f"aws: skipping {region}: {exc}")
            continue
        prices.update(region_prices)
        _log(f"aws: {region}: {sum(len(v) for v in region_prices.values())} instance types")
    return prices


def fetch_azure(regions: Sequence[str], keep: Optional[set[str]]) -> Prices:
    prices: Prices = {}
    for region in regions:
        query = f"serviceName eq 'Virtual Machines' and priceType eq 'Consumption' and armRegionName eq '{region}'"
        url: Optional[str] = f"{AZURE_API}?{urllib.parse.urlencode({'$filter': query})}"
        items: List[dict] = []
        while url:
            page = _get_json(url)
            items.extend(page.get("Items", []))
            url = page.get("NextPageLink")
        region_prices = parse_azure_items(items, keep)
        prices.update(region_prices)
        _log(f"azure: {region}: {sum(len(v) for v in region_prices.values())} instance types")
    return prices


def fetch_ovh(keep: Optional[set[str]]) -> Tuple[Prices, str]:
    _log(f"ovh: downloading {OVH_CATALOG}")
    return parse_ovh_catalog(_get_json(OVH_CATALOG), keep)


def fetch_scaleway(keep: Optional[set[str]]) -> Tuple[Prices, str]:
    products: List[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"product_types": "instance", "page_size": 100, "page": page})
        body = _get_json(f"{SCALEWAY_CATALOG}?{query}")
        batch = body.get("products", [])
        products.extend(batch)
        if not batch or len(products) >= int(body.get("total_count", 0)):
            break
        page += 1
    _log(f"scaleway: {len(products)} catalog products")
    return parse_scaleway_products(products, keep)


# --------------------------------------------------------------------------- #
# Output                                                                       #
# --------------------------------------------------------------------------- #


def render_catalog(provider: str, prices: Prices, *, currency: str, source: str, generated_at: str) -> str:
    """Renders a catalog file: one line per region keeps diffs reviewable."""
    header = {
        "schema_version": 1,
        "provider": provider,
        "currency": currency,
        "price_type": "on_demand",
        "operating_system": "linux",
        "columns": ["hourly", "vcpu", "memory_gib"],
        "source": source,
        "generated_at": generated_at,
        "generator": "scripts/generate_pricing_catalog.py",
    }
    lines = ["{"]
    for key, value in header.items():
        lines.append(f"  {json.dumps(key)}: {json.dumps(value)},")
    if not prices:
        lines.append('  "regions": {}')
    else:
        lines.append('  "regions": {')
        regions = sorted(prices)
        for index, region in enumerate(regions):
            rows = {
                name: [round(row[0], 6), row[1], round(row[2], 3) if row[2] is not None else None]
                for name, row in sorted(prices[region].items())
            }
            suffix = "," if index < len(regions) - 1 else ""
            lines.append(f"    {json.dumps(region)}: {json.dumps(rows, separators=(',', ':'))}{suffix}")
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _write(provider: str, prices: Prices, currency: str, source: str, output_dir: Path) -> None:
    if not prices:
        _log(f"{provider}: no prices fetched; keeping the existing catalog file")
        return
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    path = output_dir / f"{provider}.json"
    path.write_text(render_catalog(provider, prices, currency=currency, source=source, generated_at=generated_at))
    _log(f"{provider}: wrote {sum(len(v) for v in prices.values())} entries to {path}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--provider",
        action="append",
        choices=["aws", "azure", "ovh", "scaleway", "gcp"],
        required=True,
        help="Provider catalog to regenerate (repeatable).",
    )
    parser.add_argument("--regions", default="", help="Comma-separated regions (aws, azure). Default: mapped regions.")
    parser.add_argument("--all-instance-types", action="store_true", help="Keep instance types without power profile.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)

    for provider in dict.fromkeys(args.provider):
        filtered = provider in FILTERED_PROVIDERS and not args.all_instance_types
        keep = known_instance_types(provider) if filtered else None
        regions = [r.strip() for r in args.regions.split(",") if r.strip()] or mapped_regions(provider)
        if provider == "aws":
            source = AWS_REGION_CSV.replace("{region}", "<region>")
            _write(provider, fetch_aws(regions, keep), "USD", source, args.output_dir)
        elif provider == "azure":
            _write(provider, fetch_azure(regions, keep), "USD", AZURE_API, args.output_dir)
        elif provider == "ovh":
            prices, currency = fetch_ovh(keep)
            _write(provider, prices, currency, OVH_CATALOG, args.output_dir)
        elif provider == "scaleway":
            prices, currency = fetch_scaleway(keep)
            _write(provider, prices, currency, SCALEWAY_CATALOG, args.output_dir)
        else:
            _log("gcp: not supported (the Cloud Billing Catalog API requires an API key); keeping the empty catalog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
