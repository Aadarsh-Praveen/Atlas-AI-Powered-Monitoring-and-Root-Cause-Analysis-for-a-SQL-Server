# observability/alerts

The alert rules for ShopLite, one file each, with everything needed to create them by hand in the
Azure portal: the KQL query, threshold, how often it runs, how far back it looks, severity, and how it
behaves outside business hours. The rules themselves live in Azure; these files are their versioned
definitions. When a rule changes in Azure, change its file here in the same sitting.

**Detection is simple and independent of Atlas** (Phase 5 decision): these rules fire even if Atlas is
down. Azure Monitor sends the emails; Atlas will explain alerts (Phase 7), never decide whether to send them.

## Create them in this order

1. [`action-group.md`](action-group.md): `ag-shoplite-email`, and test it.
2. [`business-hours-suppression.md`](business-hours-suppression.md): the alert processing rule.
3. Every rule below. The log search rules need data in their tables (`observability/azure-setup.md`
   first); the two `api-` rules need API 1.2.0 (B4) sending to Application Insights.
4. `observability/test-alert.md`: one end-to-end test.

## The rules

| File | Rule name | Signal | Every | Sev | Outside business hours |
|---|---|---|---|---|---|
| [sql-blocking](sql-blocking.md) | `shoplite-sql-blocking` | `SqlBlocking_CL` | 5 min | 2 | suppressed |
| [sql-deadlocks](sql-deadlocks.md) | `shoplite-sql-deadlocks` | `SqlXEvents_CL` | 15 min | 2 | suppressed |
| [sql-log-backup-age](sql-log-backup-age.md) | `shoplite-sql-log-backup-age` | `SqlDatabases_CL` | 15 min | 2 | suppressed |
| [sql-full-backup-missing](sql-full-backup-missing.md) | `shoplite-sql-full-backup-missing` | `SqlDatabases_CL` | 1 h | 2 | suppressed |
| [sql-job-failed](sql-job-failed.md) | `shoplite-sql-job-failed` | `SqlAgentJobRuns_CL` | 15 min | 2 | suppressed |
| [sql-restore-test-failed](sql-restore-test-failed.md) | `shoplite-sql-restore-test-failed` | `DbaRestoreTests_CL` | 15 min | 2 | suppressed |
| [sql-restore-test-missed](sql-restore-test-missed.md) | `shoplite-sql-restore-test-missed` | `DbaRestoreTests_CL` | 1 h | 2 | suppressed |
| [sql-log-full](sql-log-full.md) | `shoplite-sql-log-full` | `SqlDatabases_CL` | 15 min | 2 | suppressed |
| [ops-etl-failed](ops-etl-failed.md) | `shoplite-ops-etl-failed` | `OpsEtlRuns_CL` | 15 min | 2 | suppressed |
| [ops-vendor-feed-stale](ops-vendor-feed-stale.md) | `shoplite-ops-vendor-feed-stale` | `VendorFeed_CL` | 15 min | 3 | suppressed |
| [ops-refresh-failed](ops-refresh-failed.md) | `shoplite-ops-refresh-failed` | `OpsRefreshRuns_CL` | 15 min | 3 | suppressed |
| [vm-cpu-credits](vm-cpu-credits.md) | `shoplite-vm-cpu-credits` | VM metric | 5 min | 2 | suppressed |
| [vm-data-disk](vm-data-disk.md) | `shoplite-vm-data-disk` | `Perf` (agent) | 15 min | 2 | suppressed |
| [api-error-rate](api-error-rate.md) | `shoplite-api-error-rate` | `AppRequests` | 15 min | 2 | suppressed |
| [api-latency](api-latency.md) | `shoplite-api-latency` | `AppRequests` | 15 min | 2 | suppressed |
| [api-health-availability](api-health-availability.md) | `shoplite-api-health` | ping test (free) | 5 min | 2 | suppressed |
| [monitoring-collector-heartbeat](monitoring-collector-heartbeat.md) | `shoplite-monitoring-collector-heartbeat` | `CollectorHeartbeat_CL` | 5 min | 2 | suppressed |
| [traffic-shoppers-missing](traffic-shoppers-missing.md) | `shoplite-traffic-shoppers-missing` | `AppRequests` (`playwright/`) | 1 h | 3 | suppressed (and only fires 15:00–23:00 UTC on weekdays) |
| [always-vm-running-off-hours](always-vm-running-off-hours.md) | `shoplite-always-vm-running-off-hours` | `Heartbeat` (agent) | 15 min | **1** | **always sent** |
| [always-daily-cap](always-daily-cap.md) | `shoplite-always-daily-cap` | `_LogOperation` | 15 min | **1** | **always sent** |

## Conventions

- **Names:** `shoplite-<area>-<what>`; the always-on rules are `shoplite-always-...`.
- **Severity:** **1** = always-on (any hour); **2** = needs attention during business hours; **3** =
  can wait (no shopper impact yet). The suppression rule silences 2–4 outside business hours.
- **Symptoms, not causes:** each rule watches something that's wrong for the business (blocking,
  errors, a missed backup), not a statistic that might be fine. The "First checks" in each file are the
  start of the investigation, which is what Atlas will automate later.
- **Cheap shapes:** no dimensions (one time series per rule), and the slowest frequency that still
  catches the problem in time. Log search alerts are priced by frequency; 1-minute rules cost the most
  and none is used.
- **Every query handles "no rows":** a `summarize` without `by` returns one row even when nothing
  matched, so each query ends with a `where` that removes that empty row; "missing" checks use
  `print ... toscalar(...)` so they can fire on the absence of data.
- **Duplicates:** event-table queries group by the table's `unique_id` (the collector delivers at least
  once; `observability/schema.yml`).
- **Lookback max 2 days:** a log search alert scans at most 48 hours, whatever the query says.

## Creating a log search alert (most rules)

**Monitor** → **Alerts** → **Create** → **Alert rule**:

| Tab | Field | Value |
|---|---|---|
| Scope | Resource | the workspace `law-shoplite-prod` |
| Condition | Signal | **Custom log search** |
| Condition | Search query | the rule file's KQL (paste it as is) → **Run** to see it works (it usually returns no rows: nothing is wrong) |
| Condition | Measure / Aggregation type | **Table rows** / **Count** |
| Condition | Aggregation granularity | the rule's lookback |
| Condition | Split by dimensions | none |
| Condition | Operator / Threshold | **Greater than** / **0** |
| Condition | Frequency of evaluation | the rule's "every" |
| Condition | Number of violations | 1 of 1 (Advanced options) |
| Condition | Override query time range | the rule's lookback, when it's longer than the frequency |
| Actions | Action group | `ag-shoplite-email` |
| Details | Severity / Alert rule name / Description | from the file (the description: its "Meaning" line) |
| Details | Region | North Central US |
| Details | **Automatically resolve alerts** | **On** (stateful: one email when it starts, one when it ends) |
| Details | Mute actions | Off |
| Tags | | `project=atlas`, `env=prod`, `app=shoplite` |

The metric alert (`vm-cpu-credits`) and the availability test have their own steps in their files.

## Cost

Log search alerts are billed per rule per month by how often they run: roughly **$1.50 at 5 minutes
and $0.50 at 15 minutes** (owner's figures; confirm in **https://azure.microsoft.com/pricing/calculator**,
Azure Monitor → Alert rules, and record the real total in the runbook in B7). So only the two rules
where minutes matter run every 5 minutes; everything else every 15 minutes or slower.

| Kind | Rules | Rough monthly cost |
|---|---|---|
| Log search, every 5 min | 2: **blocking** (shoppers are stuck right now), **collector heartbeat** (every other alert is blind without it) | about $3.00 |
| Log search, every 15 min | 13: deadlocks, job failed, log full, API errors, API latency, log backup age, restore failed, ETL, vendor, refresh, data disk, VM off-hours, daily cap | about $6.50 |
| Log search, every 1 h | 3: full backup missing, restore missed, shoppers missing (added 2026-11-25) | at most about $1.50 (no more than the 15-minute price) |
| Metric | 2: CPU credits; the availability test's alert | within the free monthly time series |
| Availability test | 1 classic URL ping test | free |
| **Total** | | **about $10.50 a month** |

What the 15-minute rules give up: a deadlock burst, a failed job, a filling log, or an API error or
latency spike is reported up to about 15 minutes later than at 5 minutes. The CPU-credits alert stays
a metric alert evaluated every 5 minutes (metric alerts are priced per time series, not by frequency).
Change a rule's frequency only together with its file here.

## Atlas (Phase 7)

The Atlas API's webhook is added to `ag-shoplite-email` then; the common alert schema is already on.
Atlas files its investigation under the alert's ID, so the email and the dashboard incident match.
