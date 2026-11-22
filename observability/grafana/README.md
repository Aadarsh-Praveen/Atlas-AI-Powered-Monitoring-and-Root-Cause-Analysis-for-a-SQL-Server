# observability/grafana

ShopLite's Grafana dashboards, **as code**. Grafana Cloud only *shows* data: it stores nothing, and
queries ADX (history) and Log Analytics (recent data, Application Insights, the agent) live.

| File | Dashboard | Answers | Default range |
|---|---|---|---|
| `overview.json` | **ShopLite: Overview** | Is everything healthy right now? Collector heartbeat, API traffic, errors and p95, blocking, log backup age, CPU credits, waits, failed jobs | 24 h |
| `sql-server.json` | **ShopLite: SQL Server** | What is SQL Server doing? Waits, workload, memory, sessions by program, blocking and head blockers, deadlocks, top queries (with plan counts), log usage, file latency, database sizes | 24 h |
| `operations.json` | **ShopLite: Operations** | Did the routine work? Job outcomes, backup age and sizes, restore tests (RTO), ETL runs, vendor freshness, ReportingDB refreshes, maintenance time | 7 days |
| `api.json` | **ShopLite: API** | How does the API behave? Requests by source (real vs synthetic), latency, server errors, endpoints, dependencies (SQL, ranking service), slowest statements, exceptions | 24 h |
| `vm.json` | **ShopLite: VM** | How is the machine? CPU credits and CPU, process CPU, memory, free disk, disk latency, the collector's own health, Windows and SQL Server events | 24 h |

## Don't edit the JSON by hand

The JSON files are **generated** by `observability/tools/make_dashboards.py`, where each panel is a few
lines: title, data source, KQL, unit. To change a dashboard:
1. edit the panel in `make_dashboards.py`;
2. `python observability/tools/make_dashboards.py` (from the repo root; any Python 3);
3. re-import the JSON (below): same `uid`, so it **replaces** the dashboard in Grafana.

A change made only in Grafana's editor is lost at the next import. (Exploring in the editor is fine;
copy the final query back into the generator.)

## Data sources

Set up once, in `observability/adx-grafana-setup.md` step 5: **`ADX shoplite`** (Azure Data Explorer)
and **`Azure Monitor shoplite`** (Azure Monitor), both signing in as the read-only
`shoplite-grafana-reader` app registration:

| Panel source | Data source | Role it relies on |
|---|---|---|
| History: the collector's tables over time | `ADX shoplite` | ADX **viewer** on `shoplite` |
| Now: recent collector data, `AppRequests`/`AppDependencies`/`AppExceptions`, `Perf`, `Event` | `Azure Monitor shoplite` (Logs) | **Log Analytics Reader** on `law-shoplite-prod` |
| VM metrics: CPU credits, CPU | `Azure Monitor shoplite` (Metrics) | **Monitoring Reader** on `prod-sql-01` (step 4d) |

No data source ID is written into the files: each dashboard picks its data sources through
variables, so the same JSON works in any Grafana stack.

## Import (Part C3, step 3)

Once per file, in Grafana:

1. **Dashboards** → **New** → **Import** → **Upload dashboard JSON file** → choose the file → folder:
   create **ShopLite** the first time → **Import**.
2. At the top of the dashboard, set the **variables**:

   | Variable | Value |
   |---|---|
   | History (ADX) | `ADX shoplite` |
   | Now (Azure Monitor) | `Azure Monitor shoplite` |
   | Log Analytics workspace resource ID | from `law-shoplite-prod` → JSON View → `id` (`azure-setup.md` step 1c) |
   | Subscription ID | the subscription holding `rg-shoplite-prod` |

3. **Save dashboard** (top right), and tick **Save current variable values as dashboard default**, so
   they stick.

After a re-import (a newer version from the generator), set the variables again and save: the import
brings back the placeholders.

## Reading them

- **Time zone is UTC**, like every log and schedule. The VM runs 13:00–23:00 UTC on weekdays, so
  nights and weekends are empty on purpose.
- **ADX panels lag about 5 minutes** (the collector's queued ingestion); the Overview's "right now"
  stats come from Log Analytics for that reason.
- **API counts use `sum(ItemCount)`**: Application Insights keeps a sample (25%), each row standing
  for `ItemCount` requests.
- **Event tables are de-duplicated** by their `unique_id` (at-least-once delivery).
- Panels with a description show an **(i)**: hover it for what's normal and when the matching alert fires.

| Problem | Likely cause |
|---|---|
| Every panel "No data" | Variables not set (still placeholders), or the time range covers only nights/weekends |
| ADX panels empty, others fine | `ADX shoplite` data source broken (secret expired?), or no data in ADX yet (`AdxOk` in the heartbeat) |
| Log Analytics panels: "unauthorized" | The reader app lacks Log Analytics Reader on the workspace, or the workspace resource ID variable is wrong |
| CPU credits/CPU panels empty | No Monitoring Reader on the VM (setup step 4d), or the subscription ID variable is wrong. If still empty: edit the panel, re-select the VM and metric once in the query editor, save, and tell Claude Code (the generator's metric format may need adjusting to your Grafana version) |
| API panels empty | API 1.2.0 not deployed yet, or `APPLICATIONINSIGHTS_CONNECTION_STRING` not set on the VM |

## Forecasts

Forecast panels are added in the forecasting phase, from ADX's hourly rollups (the materialized views
in `observability/adx/001_shoplite_database.kql`).
