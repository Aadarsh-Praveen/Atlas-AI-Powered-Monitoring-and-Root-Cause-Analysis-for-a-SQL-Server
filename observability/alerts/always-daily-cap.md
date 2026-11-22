# Log Analytics daily cap reached (always on)

| Setting | Value |
|---|---|
| Rule name | `shoplite-always-daily-cap` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **1: NOT suppressed** |
| Frequency / lookback | every 15 minutes / 1 hour |
| Condition | Table rows, greater than 0 |

**Meaning:** the workspace hit its daily cap (0.5 GB/day, azure-setup.md step 1b). Log Analytics stops
accepting data for the rest of the day, so **every other alert goes silent**: the dangerous kind of
failure, because nothing looks wrong.

```kusto
_LogOperation
| where TimeGenerated > ago(1h)
| where Category =~ "Ingestion" and Detail contains "OverQuota"
| project TimeGenerated, Operation, Detail
```

`_LogOperation` is the workspace's own health log (Microsoft's documented query for this); it keeps
recording after the cap stops normal data.

**Why not suppressed:** the cap can be hit at any time, and its effect lasts until the daily reset.

**First checks:** the `Usage` query (azure-setup.md, section 9) for the table that grew; a collector
bug sending in a loop, Application Insights sampling switched off, or an incident flooding the XE or
event logs. Raise the cap only after finding the cause.
