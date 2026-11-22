# Transaction log over 70% full

| Setting | Value |
|---|---|
| Rule name | `shoplite-sql-log-full` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 20 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** a database's transaction log is more than 70% used. If it fills, every write in that
database fails (error 9002) until space is freed.

```kusto
SqlDatabases_CL
| where TimeGenerated > ago(20m) and isnotnull(LogUsedPercent)
| summarize arg_max(TimeGenerated, LogUsedPercent, LogReuseWait, LogSizeMB) by DatabaseName
| where LogUsedPercent > 70
```

**Why 70%:** enough headroom to act before writes fail, while normal activity between 15-minute log
backups stays well below it. Checked every 15 minutes (a cost choice): a log that fills from 70% to 100%
in under 15 minutes is unusual for SalesDB, and the log-backup-age alert covers the usual cause.

**First checks:** `LogReuseWait` says why the log can't be reused: `LOG_BACKUP` (log backups failing or
late: see the log backup alert), `ACTIVE_TRANSACTION` (a long open transaction: look at the head
blockers and `TransactionSeconds` in `SqlBlocking_CL`, and `OpenTransactionSessions` in
`SqlSessions_CL`).
