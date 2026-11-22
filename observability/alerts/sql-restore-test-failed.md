# Restore test failed

| Setting | Value |
|---|---|
| Rule name | `shoplite-sql-restore-test-failed` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 1 hour |
| Condition | Table rows, greater than 0 |

**Meaning:** the weekly restore test (Wednesday 20:00 UTC) couldn't restore SalesDB's backups, or
CHECKDB found a problem in the restored copy. Until fixed, there's no proof the backups restore.

```kusto
DbaRestoreTests_CL
| where TimeGenerated > ago(1h) and Status == "failed"
| summarize arg_max(TimeGenerated, ErrorMessage, FullBackupFinishedAt, LogBackupsRestored) by RunId
```

**First checks:** `ErrorMessage`. "The log chain is broken" means a log backup is missing (see the LOG
job's history around that time) or someone switched to SIMPLE; CHECKDB errors mean possible corruption
(runbook, backups section: keep every backup, check SalesDB itself).

**Older than 8 days:** a log search alert can look back at most 2 days, so "no restore test in 8 days"
is its own rule: `sql-restore-test-missed.md`.
