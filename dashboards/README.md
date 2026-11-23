# GreenKube Grafana dashboard

`greenkube-operations.json` is **GreenKube Operations**, a small dashboard that shows the health of a GreenKube
install and its savings, read from the GreenKube Prometheus metrics. It needs Grafana 10 or later, a Prometheus data
source and no plugin. It is published on Grafana.com as dashboard
[25377](https://grafana.com/grafana/dashboards/25377/).

Footprint data (cost, CO₂e and energy per pod, namespace and node) is not exported to Prometheus. Use the GreenKube
Footprint pages and their CSV or JSON exports for it.

## Prerequisites

Prometheus must scrape the GreenKube API and controller. With the Prometheus Operator, set
`monitoring.serviceMonitor.enabled=true` in the Helm values. That creates the API and controller ServiceMonitors,
and the optional PrometheusRule uses the same metrics ([Prometheus and Grafana](../docs/prometheus-grafana.md)).

## Import

**From Grafana.com:** go to Dashboards → New → Import, enter `25377`, then choose your Prometheus data source.

**From this file, in the UI:** go to Dashboards → New → Import → Upload dashboard JSON file, select
`greenkube-operations.json`, then choose your Prometheus data source.

**From this file, with the HTTP API** (a local Grafana, for example):

```bash
python3 - <<'EOF'
import base64, json, urllib.request

with open("dashboards/greenkube-operations.json") as f:
    dashboard = json.load(f)
payload = json.dumps({
    "dashboard": dashboard,
    "folderId": 0,
    "overwrite": True,
    "inputs": [{"name": "DS_PROMETHEUS", "type": "datasource", "pluginId": "prometheus",
                "value": "<PROMETHEUS_DATASOURCE_UID>"}],
}).encode()
request = urllib.request.Request(
    "http://localhost:3000/api/dashboards/import",
    data=payload,
    headers={"Content-Type": "application/json",
             "Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()},
)
with urllib.request.urlopen(request) as response:
    print(json.load(response).get("importedUrl"))
EOF
```

Use `/api/dashboards/import`, not `/api/dashboards/db`. Only the import endpoint replaces `${DS_PROMETHEUS}`
with your data source. To find the data source UID, open Connections → Data sources → Prometheus; the UID is in the URL.
The JSON has no fixed `uid`, so Grafana assigns one on the first import. With `overwrite`, a later import replaces
the dashboard that has the same title in the same folder.

## Variables

| Variable | What it does |
|---|---|
| Prometheus | The data source. |
| Namespace | The namespace GreenKube runs in. It is a target label, so it scopes every panel. |
| Cluster | The GreenKube cluster name (`CLUSTER_NAME`). Only the API series carry it: the collection data age and the savings panels. |

Controller series have no `cluster` label. Panels that read them are titled *(all clusters)*. When one Prometheus
scrapes several GreenKube installs, use **Namespace** to tell them apart.

## Rows

A text panel at the top, **About GreenKube**, says what GreenKube does, with links to
[docs.greenkube.cloud](https://docs.greenkube.cloud) and the source. The rows below follow it.

| Row | What it shows |
|---|---|
| **Collection** | Age of the newest stored metric row (stale above 15 minutes). Collection watermark lag and coverage per source, from the leader controller. Windows abandoned beyond `COLLECTION_MAX_BACKFILL` and windows stored without a working Kubernetes list. |
| **Loops and scans** | Whether a controller holds the Lease. Consecutive failures per loop (the alert fires at 3) and time since each loop's last success. Time since the last successful recommendation scan, scans by status, and failures by recommendation source. |
| **Measurement and ledger** | Time since the change measurement loop last succeeded. Status of the savings ledger hash chain and of the v1/v2 reconciliation. Ledger projection backlog, and ledger write and chain verification errors. |
| **Savings (ledger v2)** | Net cost and CO₂e savings accrued by method (attributed minus reversed), and the measured share. Only `measured` is realized savings; never add methods together. Each series takes `max` before the `sum`, because every API replica serves the same counters. |
| **Automation** | Backlog of plan operations, plans and webhook deliveries by queue and state. The stuck and failed states raise alerts. |

## Editing

Edit `greenkube-operations.json` directly, or export it from Grafana with **Export for sharing externally** turned on,
which keeps the `${DS_PROMETHEUS}` input. Remove the `uid` field before you commit. `tests/grafana/test_operations_dashboard.py`
checks the following:

- every queried metric is registered on a registry that GreenKube serves;
- the savings series are deduplicated across API replicas;
- every panel is scoped by Namespace, and is either scoped by Cluster or titled *(all clusters)*;
- the file has what Grafana.com needs.

## Publishing on Grafana.com (maintainer)

The dashboard replaces the old **GreenKube FinGreenOps dashboard** as a new revision of the same Grafana.com
listing, so existing links and imports by ID keep working.

1. Sign in to grafana.com with the account that owns dashboard 25377, and open
   [My Account → Dashboards](https://grafana.com/orgs/) → *GreenKube FinGreenOps dashboard*.
2. Choose **Upload a new revision** and upload `dashboards/greenkube-operations.json` from the release tag.
3. Rename the listing to **GreenKube Operations** and replace the description with the text below.
4. Replace the screenshots with one of the new dashboard, taken on a cluster with at least a day of data.
5. Save, then open the public page and import revision `latest` into an empty Grafana to check it.

Description to paste:

> **GreenKube Operations** shows the health of a [GreenKube](https://docs.greenkube.cloud) install and the
> savings it has proven. GreenKube is an open-source Kubernetes optimization loop. It recommends CPU and memory
> requests and limits from its own measurements and delivers them as reviewed GitOps pull requests. It then
> measures what each merged change actually saved in cost, CO₂e and released capacity, and records it in an
> append-only, hash-chained ledger.
>
> Rows: collection freshness, watermark lag and coverage; controller loops and the leader; recommendation scans;
> change measurement and ledger integrity (hash chain, reconciliation, projection backlog); net savings by method
> (measured, projected, user declared) from the ledger counters; automation backlog.
>
> Requirements: GreenKube 0.4 or later, with the API and controller scraped by Prometheus (Helm
> `monitoring.serviceMonitor.enabled=true`). Grafana 10 or later, no plugin.
>
> This revision replaces the former FinGreenOps dashboard. Its per-pod footprint and sustainability score panels
> were removed together with those metrics. Footprint data is in the GreenKube UI and its CSV and JSON exports.
>
> Docs: https://docs.greenkube.cloud · Source: https://github.com/GreenKubeCloud/GreenKube
