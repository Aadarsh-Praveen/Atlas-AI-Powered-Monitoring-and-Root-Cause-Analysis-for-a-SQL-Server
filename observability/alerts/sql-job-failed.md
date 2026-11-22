# A SQL Agent job failed

| Setting | Value |
|---|---|
| Rule name | `shoplite-sql-job-failed` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 20 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** a SQL Agent job run finished with "failed": a backup, the ETL, a refresh, maintenance, the
restore test, or cleanup.

```kusto
SqlAgentJobRuns_CL
| where TimeGenerated > ago(20m) and Outcome == "failed"
| summarize arg_max(TimeGenerated, JobName, FailedStepName, FailedStepMessage, Message) by HistoryId
| project TimeGenerated, JobName, FailedStepName, FailedStepMessage
```

**Why a 20-minute lookback with a 15-minute frequency:** a job run arrives with its finish time, but
the collector sends job runs every 5 minutes and Log Analytics needs a few minutes more, so the row can
show up 10 minutes "late". Looking back a little further than the frequency means no row falls
between two checks; the stateful rule (auto-resolve) keeps a failure from notifying twice.
`by HistoryId` drops duplicate deliveries.

**First checks:** `FailedStepName` and `FailedStepMessage` in the alert's results. The ETL and refresh
also have their own alerts with more detail; for backups see `DbaCommandLog_CL`.
