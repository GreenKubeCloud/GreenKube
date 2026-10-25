# Wattnet: alternative grid intensity provider

[Wattnet](https://wattnet.eu) is an open-source service funded by the European
Union (Horizon Europe / GreenDIGIT) that tracks the environmental footprint of
electricity across Europe. It provides **free, real-time, historical and
forecasted carbon intensity data for 52 European zones** at 15-minute
resolution, plus water footprint data (not yet integrated — see the roadmap
below).

Since GreenKube 0.3.x, grid carbon intensity is provider-agnostic: you can use
**Electricity Maps** (default) or **Wattnet** as the electricity data source
without touching the pipelines.

## Why Wattnet?

- **Free** for all EU countries. You only need to create an account and
  authenticate.
- **Financed by the EU** (Horizon Europe, GreenDIGIT project), developed by
  researchers with no commercial constraints.
- **15-minute granularity** (finer than Electricity Maps' hourly data).
- Covers European zone granularity (ENTSO-E bidding zones, e.g. `IT_NORTH`,
  `SE3`, `NO1`).
- **Carbon and water footprint** in one API. Water footprint integration is
  planned for GreenKube (see roadmap).

## How it works

1. GreenKube maps the cluster's cloud zone to an Electricity Maps zone code
   (unchanged logic).
2. The Wattnet collector translates that code to the Wattnet zone naming.
3. It obtains a short-lived Bearer token from the Wattnet token service using
   your account credentials (tokens are valid for 1 day and are cached and
   refreshed automatically).
4. It fetches the **carbon footprint** (`/v1/footprints`, `footprint_type=carbon`,
   `scope=life-cycle`, `use_global=true`) for a window around the requested
   timestamp.
5. Data quality flags (`zone_status`, `valid`) are translated into GreenKube's
   `isEstimated` / `estimationMethod` fields and the records are stored in the
   same repository used for Electricity Maps data.

Zones not covered by Wattnet (e.g. `US-CAL-CISO`) automatically fall back to
the static default grid intensity map, exactly like Electricity Maps does when
its token is missing or the API fails.

### Zone mapping

Most EU country codes are identical in both naming schemes (`FR`, `DE`, `ES`,
`NL`, ...) and pass through automatically. The following table lists **every
European Electricity Maps zone code** used by GreenKube and its Wattnet
counterpart. Country-level codes that have no Wattnet aggregate are mapped to
the most representative sub-zone (largest demand area).

| Electricity Maps | Wattnet | Notes |
|------------------|---------|-------|
| `AT`, `BE`, `BG`, `CH`, `CY`, `CZ`, `DE`, `EE`, `ES`, `FI`, `FR`, `GB`, `GE`, `GR`, `HR`, `HU`, `IE`, `LT`, `LU`, `LV`, `MD`, `ME`, `MK`, `NL`, `PL`, `PT`, `RO`, `RS`, `SI`, `SK`, `TR`, `XK` | same code | Identical country codes — pass through |
| `IT` | `IT_NORTH` | Country-level default (Milan area) |
| `IT-NO` | `IT_NORTH` | Northern Italy |
| `IT-CNO` | `IT_CNORTH` | Central-Northern Italy |
| `IT-CSO` | `IT_CSOUTH` | Central-Southern Italy |
| `IT-SO` | `IT_SOUTH` | Southern Italy |
| `IT-SAR` | `IT_SARDINIA` | Sardinia |
| `IT-SIC` | `IT_SICILY` | Sicily |
| `SE` | `SE3` | Country-level default (Stockholm area) |
| `SE-SE1` | `SE1` | Luleå |
| `SE-SE2` | `SE2` | Sundsvall |
| `SE-SE3` | `SE3` | Stockholm |
| `SE-SE4` | `SE4` | Malmö |
| `NO` | `NO2` | Country-level default (Oslo/Southern Norway) |
| `NO-NO1` | `NO1` | Oslo |
| `NO-NO2` | `NO2` | Southern Norway |
| `NO-NO3` | `NO3` | Central Norway |
| `NO-NO4` | `NO4` | Northern Norway |
| `NO-NO5` | `NO5` | Western Norway |
| `DK` | `DK1` | Country-level default (mainland/Jutland) |
| `DK-DK1` | `DK1` | Western Denmark |
| `DK-DK2` | `DK2` | Eastern Denmark |
| `NI` | `NIE` | Northern Ireland |
| `GB-NIR` | `NIE` | Northern Ireland |
| `DK-BHM`, `ES-CE`, `ES-CN-*`, `ES-IB-*`, `ES-ML`, `FR-COR`, `GB-ORK`, `PT-MA` | — | Islands/exclaves not covered by Wattnet → static fallback |

**All associations are automatic.** Cloud regions outside Europe (US, Asia,
South America, Australia, ...) have no Wattnet counterpart and fall back to
the static default grid intensity, exactly like Electricity Maps does when its
token is missing.

Full list of Wattnet zones (52): Austria, Belgium, Bosnia and Herzegovina,
Bulgaria, Croatia, Cyprus, Czechia, Denmark (DK1/DK2), Estonia, Finland,
France, Georgia, Germany, Great Britain, Greece, Hungary, Ireland, Italy
(7 zones), Latvia, Lithuania, Luxembourg, Moldova, Montenegro, Netherlands,
North Macedonia, Northern Ireland, Norway (NO1–NO5), Poland, Portugal,
Romania, Serbia, Slovakia, Slovenia, Spain, Sweden (SE1–SE4), Switzerland,
Turkey, and Kosovo.

## Prerequisites

1. Create a free account on the Wattnet token service:
   <https://api.wattnet.eu/token-request/register>
   (`POST /token-request/register` with `{"email": ..., "password": ...}`).
2. Keep the **email/password** — they are used to obtain API tokens, not the
   documentation site login (which currently only supports GitHub/Google
   social login).

> Note: The Wattnet API is currently still under development, and authorization is required to create an account. To do so, please contact iglesias@ifca.es.

## Configuration

Set the provider and credentials:

```yaml
# helm values
config:
  electricityProvider: wattnet

secrets:
  wattnetEmail: "you@example.com"
  wattnetPassword: "your-password"
```

Or via environment variables:

```bash
ELECTRICITY_PROVIDER=wattnet
WATTNET_EMAIL="you@example.com"
WATTNET_PASSWORD="your-password"
```

Optional overrides (defaults shown):

```bash
WATTNET_API_BASE_URL="https://api.wattnet.eu/v1"
WATTNET_TOKEN_SERVICE_URL="https://api.wattnet.eu/token-request"
```

> Note: `ELECTRICITY_MAPS_TOKEN` is ignored while `ELECTRICITY_PROVIDER=wattnet`.

### Verifying the setup

- The health endpoint reports a `wattnet` service:
  `GET /api/v1/health/services/wattnet` should return `"healthy"`.
- The credentials can also be updated at runtime from the Settings page
  (`POST /api/v1/config/services` with `wattnet_email` / `wattnet_password`),
  and are persisted to the Kubernetes Secret.

## API endpoints used

GreenKube currently uses a subset of the Wattnet API
(docs: <https://api.wattnet.eu/v1/docs>):

| Endpoint | Purpose |
|----------|---------|
| `POST /token-request/get_token` | Obtain a Bearer token (cached, auto-refreshed daily) |
| `GET /v1/footprints` | Carbon footprint time series (`footprint_type=carbon`, `scope=life-cycle`, `use_global=true`) |

Data quality flags (`zone_status`: `complete`/`preview`/`missing`, `valid`:
`true`/`false`) are preserved in the stored records: preview or invalid data
is marked as estimated in GreenKube. Data younger than ~4h may still be
recalculated upstream (`valid=false`); it is refreshed on subsequent hourly
collection cycles.

## Data consolidation

Wattnet values are **provisional for about 4 hours** after measurement: the
underlying ENTSO-E inputs are still arriving, so the API marks recent points
as `valid=false` / `zone_status=preview` and recalculates them later. Once
consolidated, the same timestamps are returned with `valid=true` /
`zone_status=complete` and the values are final.

GreenKube handles this in two steps:

1. The **hourly intensity collection** refreshes the last 24 hours of history
   and **upserts** the revised values into `carbon_intensity_history`
   (same zone + timestamp, new value — both for Wattnet and Electricity Maps).
2. It then **recomputes every combined metric in the last 24 hours** against
   the refreshed history — updating rows that already had an intensity value,
   not only those without one. Metrics calculated from provisional values are
   therefore corrected in place once the final data is available (on the next
   hourly cycle after consolidation).

The recomputation reuses the same carbon calculator as the main pipeline, so
the corrected `grid_intensity` and `co2e_grams` are identical to what a fresh
calculation would produce. On PostgreSQL this runs as a single bulk `UPDATE`;
other backends fall back to a per-row recompute.

## Roadmap: water footprint

Wattnet also exposes **water footprint** data (`footprint_type=water`, unit
`l/kWh`) through the same `/v1/footprints` endpoint. Water footprint
integration is intentionally **not** wired into GreenKube yet.

The provider abstraction was designed with this in mind: a future
`BaseWaterProvider` (and a Wattnet implementation) can be added alongside the
existing electricity providers without affecting the carbon pipelines. Because
Electricity Maps does not provide water data, water and electricity will stay
as separate provider abstractions; a combined `ElectricityWaterProvider`
would force pairing Wattnet with another source for no benefit.

## References

- Wattnet website: <https://wattnet.eu>
- API documentation: <https://api.wattnet.eu/v1/docs>
- GitHub: <https://github.com/wattnet>
- GreenDIGIT project: <https://greendigit-project.eu/>
