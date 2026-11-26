# observability

Everything ShopLite's monitoring needs outside the collector's code (`server/collector/`): the table
definitions, the setup guides for Azure Monitor, ADX, and Grafana, the alert rules, and the
dashboards. Built in Phase 5. **You create every Azure resource by hand** from these guides; there's
no Bicep for this phase.

This page is a map. The steps themselves are in the guides it links to.

## What's here

| File or folder | What it's for |
|---|---|
| [`schema.yml`](schema.yml) | **The source of truth:** every table the collector sends, its columns and types, defined once. Log Analytics and ADX both use these names. Change a table here first. |
| [`tables/`](tables/) | **Generated** from `schema.yml`: one sample `.json` per custom table and `transformations.kql`, for the portal's "New custom log (DCR-based)" wizard |
| [`adx/001_shoplite_database.kql`](adx/001_shoplite_database.kql) | The ADX `shoplite` database: the same tables, ingestion mappings, retention, and the hourly rollups. Run in the ADX web UI. |
| [`azure-setup.md`](azure-setup.md) | Guide: Log Analytics, Application Insights, the data collection endpoint and rules, the custom tables, the VM's managed identity, the Azure Monitor Agent, cost settings |
| [`adx-grafana-setup.md`](adx-grafana-setup.md) | Guide: the free ADX cluster, the two app registrations, Grafana Cloud and its one data source (ADX through Log Analytics' `adx()`) |
| [`alerts/`](alerts/) | One file per alert rule (query, threshold, frequency, severity, behaviour outside business hours), plus the action group and the suppression rule. [`alerts/README.md`](alerts/README.md) has the list and the creation order. |
| [`grafana/`](grafana/) | [`grafana/README.md`](grafana/README.md): the dashboards, importing and reading them. The JSON itself isn't committed: the generator writes it into git-ignored `grafana/local/` (or `grafana/placeholders/`). |
| [`test-alert.md`](test-alert.md) | The end-to-end alert test: an email must arrive |
| [`tools/make_table_files.py`](tools/make_table_files.py) | Regenerates `tables/` from `schema.yml` |
| [`tools/make_dashboards.py`](tools/make_dashboards.py) | **The dashboards' source of truth:** writes import-ready `grafana/local/<name>.json` with your values from the root `.env` (`--placeholders`: `grafana/placeholders/`, no `.env`) |

**Generated files aren't edited by hand.**
- **Tables:** change `schema.yml`, run `python observability/tools/make_table_files.py`, and commit
  both (the `tables/` files are committed: the portal wizard needs them).
- **Dashboards:** change the panel definitions in `make_dashboards.py`, run
  `python observability/tools/make_dashboards.py`, and re-import. Only the generator is committed.

A schema change also means changing the collector (its tests check every record against
`schema.yml`), updating the ADX script, and updating the table in Log Analytics.

## The order to use them

This follows Part C of the Phase 5 plan.

1. **Local first.** Run the collector against local SQL Server (`server/collector/README.md`). No
   Azure is needed yet.
2. **The Log Analytics workspace** ([`azure-setup.md`](azure-setup.md) step 1; done 2026-11-22:
   `law-shoplite-prod`, 31 days, 0.5 GB/day cap). Grafana needs it, because it reads ADX **through**
   Log Analytics (`adx()`): Grafana's ADX data source is an Enterprise plugin.
3. **ADX and Grafana** ([`adx-grafana-setup.md`](adx-grafana-setup.md)). This doesn't need the VM, and
   it uses `adx/001_shoplite_database.kql`.
4. **The rest of Azure Monitor** ([`azure-setup.md`](azure-setup.md) steps 2–10). It uses `tables/`,
   and steps 5–8 need the VM running. It ends with the collector service on the VM sending data (`server/collector/README.md`,
   "On prod-sql-01").
5. **Alerts** ([`alerts/README.md`](alerts/README.md)): the action group, then the suppression rule,
   then each rule, in the order given there.
6. **Dashboards** ([`grafana/README.md`](grafana/README.md)): generate them
   (`observability/tools/make_dashboards.py`) and import `grafana/local/`.
7. **Test alert** ([`test-alert.md`](test-alert.md)).
8. **First game day:** `incidents/README.md`.

Operating it all afterwards (what each alert means, first checks, costs): the production runbook,
`knowledge/runbooks/shoplite-production.md`, sections 19–22.
