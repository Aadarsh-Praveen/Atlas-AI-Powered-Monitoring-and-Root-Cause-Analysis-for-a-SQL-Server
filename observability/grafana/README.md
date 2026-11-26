# observability/grafana

ShopLite's Grafana dashboards, **as code**. Grafana Cloud only *shows* data: it stores nothing, and
queries ADX (history) and Log Analytics (recent data, Application Insights, the agent) live, all
through **one data source**: the free Azure Monitor data source.

**No dashboard JSON is committed.** The source of truth is `observability/tools/make_dashboards.py`;
it writes the JSON on demand into git-ignored folders here (below), so there's only one set to import.

| Generated file | Dashboard | Answers | Default range |
|---|---|---|---|
| `overview.json` | **ShopLite: Overview** | Is everything healthy right now? Collector heartbeat, API traffic, errors and p95, blocking, log backup age, CPU credits, waits, failed jobs | 24 h |
| `sql-server.json` | **ShopLite: SQL Server** | What is SQL Server doing? Waits, workload, memory, sessions by program, blocking and head blockers, deadlocks, top queries (with plan counts), log usage, file latency, database sizes | 24 h |
| `operations.json` | **ShopLite: Operations** | Did the routine work? Job outcomes, backup age and sizes, restore tests (RTO), ETL runs, vendor freshness, ReportingDB refreshes, maintenance time | 7 days |
| `api.json` | **ShopLite: API** | How does the API behave? Requests by source (real vs synthetic), latency, server errors, endpoints, dependencies (SQL, ranking service), slowest statements, exceptions | 24 h |
| `vm.json` | **ShopLite: VM** | How is the machine? CPU credits and CPU, process CPU, memory, free disk, disk latency, the collector's own health, Windows and SQL Server events | 24 h |

## Change a dashboard in the generator, not in Grafana

Each panel is a few lines in `observability/tools/make_dashboards.py`: title, data source, KQL, unit.
To change a dashboard:
1. edit the panel in `make_dashboards.py`;
2. run `python observability/tools/make_dashboards.py` (from the repo root, root `.venv` active);
3. re-import `local/<name>.json` (below): same `uid`, so it **replaces** the dashboard in Grafana.

A change made only in Grafana's editor is lost at the next import. (Exploring in the editor is fine;
copy the final query back into the generator.)

## The data source, and how ADX is reached

**One data source:** `Azure Monitor shoplite`, set up once in `observability/adx-grafana-setup.md`
step 5, signing in as the read-only `app-shoplite-grafana` app registration.

**Why not Grafana's Azure Data Explorer data source:** it's an **Enterprise plugin** (Grafana Cloud
Pro/Advanced or Enterprise only), so it stops working when a Grafana Cloud trial ends. Instead, ADX
history is read **through Log Analytics** with a cross-service query: the generator wraps every
collector table in an ADX panel as

```kusto
adx("${adx_cluster}/${adx_database}").SqlWaitStats_CL | where $__timeFilter(TimeGenerated) ...
```

| Panel source | Query | Role it relies on |
|---|---|---|
| History: the collector's tables over time | Logs, `adx(...)` around each table | **Log Analytics Reader** on `law-shoplite-prod` **and** ADX **Viewer** on the database: Log Analytics runs the ADX part as the caller (observed when a guest account without Viewer was refused) |
| Now: recent collector data, `AppRequests`/`AppDependencies`/`AppExceptions`, `Perf`, `Event` | Logs | **Log Analytics Reader** on `law-shoplite-prod` |
| VM metrics: CPU credits, CPU | Metrics | **Monitoring Reader** on `prod-sql-01` (step 4d) |

Things to know about `adx()` (Microsoft's page "Correlate data in Azure Data Explorer and Azure
Resource Graph with data in a Log Analytics workspace", read 2026-11-23):
- **The time filter isn't passed to ADX**: every ADX query filters `TimeGenerated` itself. The
  generator's ADX queries all do; keep it that way when adding panels.
- **Database names are case sensitive** (`shoplite`, not `ShopLite`).
- Not supported with ADX clusters that have IP restrictions or Private Link, or across tenants.
- Results come back through the Log Analytics Query API, whose limits are 500,000 records,
  about 100 MB of raw data (64 MB compressed), and 10 minutes per query (Azure Monitor service
  limits, "Query API"). Microsoft doesn't say whether cross-service queries have lower limits; every
  panel here summarizes in ADX first, so results are small.

No data source ID, workspace, subscription, or cluster is written into the files: each dashboard gets
them from variables, so the same JSON works in any Grafana stack.

## Import (Part C3, step 3)

**Your values, once, in the root `.env`.** An import sets every variable to what's in the file, so
the generator writes your values into the files themselves:

| Root `.env` variable | Dashboard variable | Required |
|---|---|---|
| `GRAFANA_WORKSPACE_ID` | Log Analytics workspace resource ID | yes |
| `GRAFANA_SUBSCRIPTION_ID` | Subscription ID | yes |
| `GRAFANA_ADX_CLUSTER_URI` | ADX cluster URI (the query URI, not the collector's `ingest-` URI) | yes |
| `GRAFANA_ADX_DATABASE` | ADX database | no, default `shoplite` |
| `GRAFANA_DATASOURCE` | Azure Monitor data source | no, default `Azure Monitor shoplite` |

(`GRAFANA_` names, deliberately not the collector's `ADX_DATABASE`: pointing Grafana at
`shoplite_dev` must not also redirect a local collector run.) Then, with the root `.venv` active:

```bash
python observability/tools/make_dashboards.py
```

It writes `observability/grafana/local/<name>.json` (git-ignored) with your values as the variable
defaults. If a required value is missing (or still `.env.example`'s placeholder), it names it and
writes nothing.

Once per file, in Grafana:

1. **Dashboards** → **New** → **Import** → **Upload dashboard JSON file** → choose
   `local/<name>.json` → folder: create **ShopLite** the first time → **Import**.
2. Check the **variables** at the top show your values.

**No `.env` (someone else, or a fresh machine)?** `python observability/tools/make_dashboards.py
--placeholders` (any Python 3, reads no `.env`) writes `observability/grafana/placeholders/<name>.placeholders.json`
(git-ignored, and named differently on purpose: Grafana's file picker shows only the name). Import
those, then set the variables by hand:

   | Variable | Value |
   |---|---|
   | Azure Monitor | `Azure Monitor shoplite` |
   | Log Analytics workspace resource ID | from `law-shoplite-prod` → JSON View → `id` (`azure-setup.md` step 1c) |
   | Subscription ID | the subscription holding `rg-shoplite-prod` |
   | ADX cluster URI | the free cluster's URI, `https://<name>.<region>.kusto.windows.net` (ADX web UI → the cluster → copy URI) |
   | ADX database | `shoplite` (already the default; `shoplite_dev` only to look at test data) |

   Then **Save dashboard** (top right) and tick **Save current variable values as dashboard
   default**, so they stick until the next import.

After a change to the generator: run it again and re-import the `local/` files (same `uid`, so each
replaces its dashboard).

**Status colours** are fixed by meaning on every panel: succeeded green, failed red, retry orange,
cancelled grey (bars per outcome and the Status/Outcome columns of the run tables).

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
| ADX panels empty, others fine | No data in ADX yet (`AdxOk` in the heartbeat), the ADX cluster URI or database variable is wrong (database names are case sensitive), or the app lacks ADX **Viewer** on the database |
| ADX panels error, others fine | Try the same `adx(...)` query in **Explore** with the Azure Monitor data source: if it fails there too but works in the ADX web UI, cross-service queries are the problem: use the fallback below |
| Log Analytics panels: "unauthorized" | The reader app lacks Log Analytics Reader on the workspace, or the workspace resource ID variable is wrong |
| CPU credits/CPU panels empty | No Monitoring Reader on the VM (setup step 4d), or the subscription ID variable is wrong. If still empty: edit the panel, re-select the VM and metric once in the query editor, save, and tell Claude Code (the generator's metric format may need adjusting to your Grafana version) |
| API panels empty | API 1.2.0 not deployed yet, or `APPLICATIONINSIGHTS_CONNECTION_STRING` not set on the VM |

## Fallback: ADX's own dashboards

If cross-service queries ever stop working (or become unavailable for the free cluster), the history
is still there: **ADX dashboards** in the ADX web UI (https://dataexplorer.azure.com → **Dashboards**)
are free and query the same tables directly. The ADX panels' queries in `make_dashboards.py` are plain
KQL without the `adx()` wrapper, so they can be pasted in as they are (replace `$__timeFilter(TimeGenerated)`
with `TimeGenerated between (_startTime .. _endTime)`, the time range parameters ADX dashboards provide). Recent data and alerts stay in
Log Analytics either way.

## Forecasts

Forecast panels are added in the forecasting phase, from ADX's hourly rollups (the materialized views
in `observability/adx/001_shoplite_database.kql`). Microsoft's `adx()` page doesn't mention
materialized views, so reading them through Log Analytics must be tested then.
