# Log backup older than 30 minutes

| Setting | Value |
|---|---|
| Rule name | `shoplite-sql-log-backup-age` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 15 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** SalesDB's newest log backup is more than 30 minutes old. Log backups run every 15
minutes, so at least one was missed: the recovery point is worse than promised, and the log can't be
reused, so it grows.

```kusto
SqlDatabases_CL
| where TimeGenerated > ago(15m) and DatabaseName == "SalesDB"
| summarize arg_max(TimeGenerated, LastLogBackupAt, RecoveryModel, LogReuseWait)
| where RecoveryModel == "FULL" and (isnull(LastLogBackupAt) or LastLogBackupAt < ago(30m))
| extend MinutesSinceLogBackup = datetime_diff("minute", now(), LastLogBackupAt)
```

**Business hours:** the first log backup of the day runs at 13:15, so between 13:00 and 13:15 the newest
backup is still the previous day's 22:50. The suppression window ends at **13:30** for this reason.

**First checks:** the "ShopLite - Backup SalesDB LOG" runs in `SqlAgentJobRuns_CL` (failed? not running
at all?), `DbaCommandLog_CL` for `BACKUP_LOG` errors (an expired SAS shows as error 3201 or 403), and the
collector heartbeat (no fresh `SqlDatabases_CL` rows means the collector, not the backups, stopped).
