# tests/scripts/test_generate_pricing_catalog.py
"""Parsers of the PricingCatalog generator, on recorded fixture excerpts (no network)."""

import importlib.util
import json
from pathlib import Path

import pytest

from greenkube.impact.pricing import PricingCatalog

SCRIPT = Path(__file__).parents[2] / "scripts" / "generate_pricing_catalog.py"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("generate_pricing_catalog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AWS_HEADER = (
    '"SKU","OfferTermCode","RateCode","TermType","PriceDescription","EffectiveDate","StartingRange",'
    '"EndingRange","Unit","PricePerUnit","Currency","Product Family","serviceCode","Location","Location Type",'
    '"Instance Type","vCPU","Memory","Tenancy","Operating System","License Model","Pre Installed S/W",'
    '"CapacityStatus","operation","Region Code"'
)


def _aws_row(
    price, *, term="OnDemand", os="Linux", tenancy="Shared", software="NA", capacity="Used", op="RunInstances"
):
    return (
        f'"X","Y","Z","{term}","desc","2026-11-01","0","Inf","Hrs","{price}","USD","Compute Instance",'
        f'"AmazonEC2","EU (Paris)","AWS Region","m5.large","2","8 GiB","{tenancy}","{os}",'
        f'"No License required","{software}","{capacity}","{op}","eu-west-3"'
    )


def test_aws_parser_keeps_on_demand_linux_shared_prices(generator):
    lines = [
        '"FormatVersion","v1.0"',
        '"Disclaimer","..."',
        '"Publication Date","2026-11-07T00:00:00Z"',
        '"Version","1"',
        '"OfferCode","AmazonEC2"',
        AWS_HEADER,
        _aws_row("0.1120000000"),
        _aws_row("0.2000000000", os="Windows"),
        _aws_row("0.0500000000", term="Reserved"),
        _aws_row("0.0010000000", capacity="UnusedCapacityReservation"),
        _aws_row("0.3000000000", tenancy="Dedicated"),
        _aws_row("0.0000000000", op="RunInstances:0002"),
    ]
    prices = generator.parse_aws_csv(lines, keep={"m5.large"})
    assert prices == {"eu-west-3": {"m5.large": (0.112, 2.0, 8.0)}}
    assert generator.parse_aws_csv(lines, keep={"t3.micro"}) == {}


def test_azure_parser_skips_windows_spot_and_low_priority(generator):
    base = {
        "type": "Consumption",
        "serviceName": "Virtual Machines",
        "unitOfMeasure": "1 Hour",
        "currencyCode": "USD",
        "armRegionName": "westeurope",
        "armSkuName": "Standard_D2s_v5",
        "skuName": "D2s v5",
        "productName": "Virtual Machines Dsv5 Series",
    }
    items = [
        {**base, "retailPrice": 0.115},
        {**base, "retailPrice": 0.207, "productName": "Virtual Machines Dsv5 Series Windows"},
        {**base, "retailPrice": 0.02, "skuName": "D2s v5 Spot"},
        {**base, "retailPrice": 0.03, "skuName": "D2s v5 Low Priority"},
        {**base, "retailPrice": 0.05, "type": "DevTestConsumption"},
    ]
    assert generator.parse_azure_items(items, keep=None) == {"westeurope": {"Standard_D2s_v5": (0.115, None, None)}}


def test_ovh_parser_reads_hourly_consumption_plans(generator):
    catalog = {
        "locale": {"currencyCode": "EUR"},
        "addons": [
            {
                "planCode": "b3-8.consumption",
                "pricings": [{"intervalUnit": "hour", "price": 5120000}],
                "blobs": {"technical": {"cpu": {"cores": 2}, "memory": {"size": 8}}},
            },
            {"planCode": "b3-8.monthly.postpaid", "pricings": [{"intervalUnit": "month", "price": 2517000000}]},
            {"planCode": "b3-8.consumption.LZ.EU", "pricings": [{"intervalUnit": "hour", "price": 9300000}]},
        ],
    }
    prices, currency = generator.parse_ovh_catalog(catalog, keep=None)
    assert currency == "EUR"
    assert prices == {"*": {"b3-8": (0.0512, 2.0, 8.0)}}


def test_scaleway_parser_keys_by_region_and_skips_retired(generator):
    def product(zone, nanos, status="general_availability"):
        return {
            "product_category": "Instance",
            "product": "PRO2-S",
            "status": status,
            "locality": {"zone": zone},
            "unit_of_measure": {"unit": "hour", "size": 1},
            "price": {"retail_price": {"currency_code": "EUR", "units": 0, "nanos": nanos}},
            "properties": {"hardware": {"cpu": {"virtual": {"count": 8}}, "ram": {"size": 32 * 1024**3}}},
        }

    prices, currency = generator.parse_scaleway_products(
        [product("fr-par-2", 220_000_000), product("fr-par-1", 219_000_000), product("nl-ams-1", 1, "retired")],
        keep=None,
    )
    assert currency == "EUR"
    assert prices == {"fr-par": {"PRO2-S": (0.219, 8.0, 32.0)}}


def test_rendered_catalog_loads_in_pricing_catalog(generator, tmp_path):
    text = generator.render_catalog(
        "aws",
        {"eu-west-3": {"m5.large": (0.112, 2.0, 8.0)}},
        currency="USD",
        source="fixture",
        generated_at="2026-11-07T00:00:00Z",
    )
    document = json.loads(text)
    (tmp_path / "aws.json").write_text(text)
    entry = PricingCatalog(data_dir=tmp_path).lookup("aws", "eu-west-3", "m5.large")
    assert document["generated_at"] == "2026-11-07T00:00:00Z"
    assert entry is not None and entry.hourly == 0.112


def test_wildcard_region_applies_everywhere():
    catalog = PricingCatalog(
        documents={
            "ovh": {
                "schema_version": 1,
                "currency": "EUR",
                "columns": ["hourly", "vcpu", "memory_gib"],
                "regions": {"*": {"b3-8": [0.0512, 2, 8]}, "gra": {"b3-8": [0.06, 2, 8]}},
            }
        }
    )
    prices = {region: catalog.lookup("ovh", region, "b3-8") for region in ("GRA", "SBG5", None)}
    assert {region: entry.hourly if entry else None for region, entry in prices.items()} == {
        "GRA": 0.06,
        "SBG5": 0.0512,
        None: 0.0512,
    }
