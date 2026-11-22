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
| [`adx-grafana-setup.md`](adx-grafana-setup.md) | Guide: the free ADX cluster, the two app registrations, Grafana Cloud and its data sources |
| [`alerts/`](alerts/) | One file per alert rule (query, threshold, frequency, severity, behaviour outside business hours), plus the action group and the suppression rule. [`alerts/README.md`](alerts/README.md) has the list and the creation order. |
| [`grafana/`](grafana/) | **Generated** dashboards (`*.json`) to import. [`grafana/README.md`](grafana/README.md) covers importing and reading them. |
| [`test-alert.md`](test-alert.md) | The end-to-end alert test: an email must arrive |
| [`tools/make_table_files.py`](tools/make_table_files.py) | Regenerates `tables/` from `schema.yml` |
| [`tools/make_dashboards.py`](tools/make_dashboards.py) | Regenerates `grafana/*.json` from the panel definitions inside it |

**Generated files aren't edited by hand.** Change `schema.yml` (or the panel definitions in
`make_dashboards.py`), re-run the tool from the repo root, and commit both:

```bash
python observability/tools/make_table_files.py
python observability/tools/make_dashboards.py
```

A schema change also means changing the collector (its tests check every record against
`schema.yml`), updating the ADX script, and updating the table in Log Analytics.

## The order to use them

This follows Part C of the Phase 5 plan.

1. **Local first.** Run the collector against local SQL Server (`server/collector/README.md`). No
   Azure is needed yet.
2. **ADX and Grafana** ([`adx-grafana-setup.md`](adx-grafana-setup.md)). This doesn't need the VM, and
   it uses `adx/001_shoplite_database.kql`.
3. **Azure Monitor** ([`azure-setup.md`](azure-setup.md)). It uses `tables/`, and steps 5–8 need the VM
   running. It ends with the collector service on the VM sending data (`server/collector/README.md`,
   "On prod-sql-01").
4. **Alerts** ([`alerts/README.md`](alerts/README.md)): the action group, then the suppression rule,
   then each rule, in the order given there.
5. **Dashboards** ([`grafana/README.md`](grafana/README.md)): import the JSON files.
6. **Test alert** ([`test-alert.md`](test-alert.md)).
7. **First game day:** `incidents/README.md`.

Operating it all afterwards (what each alert means, first checks, costs): the production runbook,
`knowledge/runbooks/shoplite-production.md`, sections 19–22.
