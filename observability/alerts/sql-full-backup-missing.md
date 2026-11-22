# Full backup missing for a business day

| Setting | Value |
|---|---|
| Rule name | `shoplite-sql-full-backup-missing` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 1 hour / 1 hour |
| Condition | Table rows, greater than 0 |

**Meaning:** SalesDB's newest full backup is older than the last business evening's 21:30 run.
Restores then need more log backups, and the weekly restore test restores an old base.

```kusto
// Full backups run Monday-Friday at 21:30 UTC. On Monday the newest one is Friday's (about 64 hours
// old by 13:30); on other weekdays it's last night's (about 16 hours old).
let maxAge = iff(dayofweek(now()) == 1d, 74h, 26h);
SqlDatabases_CL
| where TimeGenerated > ago(15m) and DatabaseName == "SalesDB"
| summarize arg_max(TimeGenerated, LastFullBackupAt)
| where isnotnull(TimeGenerated)        // a real snapshot (if the collector is down, the heartbeat alert fires instead)
| where isnull(LastFullBackupAt) or LastFullBackupAt < ago(maxAge)
| extend HoursSinceFullBackup = datetime_diff("hour", now(), LastFullBackupAt)
```

**Why hourly:** a missing full backup isn't urgent minute by minute, and an hourly rule is the cheapest
kind. `dayofweek()` returns days since Sunday: `1d` is Monday.

**First checks:** the "ShopLite - Backup SalesDB FULL" job's last runs in `SqlAgentJobRuns_CL` and its
`BACKUP_DATABASE` rows in `DbaCommandLog_CL`; whether the VM was running at 21:30 (a VM stopped early
skips the evening jobs).
