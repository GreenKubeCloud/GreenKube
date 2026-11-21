"""Regenerate ``src/greenkube/data/electricity_maps_default.csv`` from Ember yearly data (NRG-04).

Source: Ember, "Yearly electricity data" (CC BY 4.0),
https://files.ember-energy.org/public-downloads/generation/outputs/release_generation_yearly_global.csv
Column ``Emissions intensity (gCO2e/kWh)`` of the ``Total generation`` row of each country: the annual
generation-weighted mean with Ember's life-cycle emission factors.

Usage::

    python scripts/generate_zone_intensity_defaults.py <ember.csv> <year> > data/electricity_maps_default.csv

Every Electricity Maps zone of the previous table is kept (zone, country, name). A country zone
(``FR``) gets the Ember value of its country for ``year``; a sub-zone (``US-CAL-CISO``, ``SE-SE3``)
is left empty and resolves to its country at read time (``intensity_source=country_default``).
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "src" / "greenkube" / "data"
SOURCE = "Ember yearly electricity data"
FACTOR_TYPE = "lifecycle"

#: Electricity Maps country zones whose name differs from the Ember area name: zone -> ISO 3 code.
ZONE_ISO3 = {
    "BA": "BIH",
    "BN": "BRN",
    "CD": "COD",
    "CI": "CIV",
    "GB": "GBR",
    "HK": "HKG",
    "IN": "IND",
    "LA": "LAO",
    "MO": "MAC",
    "PH": "PHL",
    "PS": "PSE",
    "TR": "TUR",
    "TW": "TWN",
    "TZ": "TZA",
    "US": "USA",
}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def ember_by_country(path: str, year: int) -> dict[str, float]:
    """``{normalized country name: gCO2e/kWh}`` for ``year`` (and ``{"iso3:XXX": value}``)."""
    values: dict[str, float] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["Electricity source"] != "Total generation" or row["Area type"] != "Country or economy":
                continue
            if int(row["Year"]) != year or not row["Emissions intensity (gCO2e/kWh)"]:
                continue
            value = round(float(row["Emissions intensity (gCO2e/kWh)"]), 1)
            values[_norm(row["Area"])] = value
            values[f"iso3:{row['ISO 3 code']}"] = value
    return values


def main(ember_path: str, year: int) -> str:
    ember = ember_by_country(ember_path, year)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["zone", "country", "name", "intensity_g_per_kwh", "year", "source", "emission_factor_type"])
    zones = _zones()
    codes = {zone for zone, _, _ in zones}
    for zone, country, name in zones:
        value = None
        # A country zone, or a sub-zone whose country has no row of its own (CL-SEN), takes the country value.
        if "-" not in zone or zone.partition("-")[0] not in codes:
            key = _norm(country)
            value = ember.get(f"iso3:{ZONE_ISO3[zone]}") if zone in ZONE_ISO3 else ember.get(key)
        if value is None:
            writer.writerow([zone, country, name, "", "", "", ""])
        else:
            writer.writerow([zone, country, name, value, year, SOURCE, FACTOR_TYPE])
    return out.getvalue()


def _zones() -> list[tuple[str, str, str]]:
    """Zones of the current table (the former ``Zone Code;Country;Zone name`` layout is accepted)."""
    text = (DATA / "electricity_maps_default.csv").read_text(encoding="utf-8")
    if text.startswith("Zone Code;"):
        rows = csv.DictReader(io.StringIO(text), delimiter=";")
        return [(r["Zone Code"].strip(), r["Country"].strip(), r["Zone name"].strip()) for r in rows if r["Zone Code"]]
    return [(r["zone"], r["country"], r["name"]) for r in csv.DictReader(io.StringIO(text))]


if __name__ == "__main__":
    sys.stdout.write(main(sys.argv[1], int(sys.argv[2])))
