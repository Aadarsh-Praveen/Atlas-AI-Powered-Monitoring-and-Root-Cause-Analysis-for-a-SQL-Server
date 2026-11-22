# Vendor ETL failed

| Setting | Value |
|---|---|
| Rule name | `shoplite-ops-etl-failed` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 30 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** today's vendor feed wasn't loaded: SalesDB keeps yesterday's supplier prices and stock.

```kusto
OpsEtlRuns_CL
| where TimeGenerated > ago(30m) and Status == "failed"
| summarize arg_max(TimeGenerated, ErrorStage, ErrorMessage, VendorNewestPriceAt) by RunId
```

Runs that fail before they can write a row (exit codes 2, 3, 4: configuration, SalesDB login, lock)
don't appear here; they fail the SQL Agent job, which `sql-job-failed` catches.

**First checks:** `ErrorStage` (connect, validate, extract, stage, merge) and `ErrorMessage`.
`validate` with "column missing" or "type expected" means the vendor changed their schema; "hours old"
means a stale feed (see `ops-vendor-feed-stale`). The runbook's ETL section has the full table.
