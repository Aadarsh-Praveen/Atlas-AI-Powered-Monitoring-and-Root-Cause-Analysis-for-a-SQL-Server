# ReportingDB refresh failed

| Setting | Value |
|---|---|
| Rule name | `shoplite-ops-refresh-failed` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **3** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 30 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** a ReportingDB refresh failed; its tables keep their previous numbers (the refresh rolls back).

```kusto
OpsRefreshRuns_CL
| where TimeGenerated > ago(30m) and Status == "failed"
| summarize arg_max(TimeGenerated, Mode, FromDate, ErrorMessage) by RunId
```

**Why severity 3:** nothing customer-facing depends on ReportingDB yet, and the next successful run
catches up the gap automatically.

**First checks:** `ErrorMessage` (runbook, ReportingDB section): a lock timeout or deadlock with live
orders, or SalesDB missing.
