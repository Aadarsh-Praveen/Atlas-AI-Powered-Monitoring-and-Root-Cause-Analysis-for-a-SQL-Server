# VM running outside business hours (always on)

| Setting | Value |
|---|---|
| Rule name | `shoplite-always-vm-running-off-hours` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **1: NOT suppressed** (the suppression rule only covers severities 2–4) |
| Frequency / lookback | every 15 minutes / 15 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** the VM is running when it should be deallocated: weeknights after 23:45 UTC, before 13:00,
or at the weekend. Usually a failed `vm-schedule.yml` deallocation, a manual start left on, or the 04:00
safety-net shutdown failing too. It costs money every hour.

```kusto
// Business hours: Monday-Friday 13:00-23:00 UTC, plus 45 minutes' grace for the 23:00 deallocation
// (GitHub can start scheduled runs late).
let day = dayofweek(now());                     // 0d = Sunday ... 6d = Saturday
let timeOfDay = now() - startofday(now());
Heartbeat                                       // the Azure Monitor Agent's own heartbeat, once a minute
| where TimeGenerated > ago(15m) and Computer startswith "prod-sql-01"
| summarize LastHeartbeat = max(TimeGenerated)
| where isnotnull(LastHeartbeat)
| where day in (0d, 6d) or timeOfDay < 13h or timeOfDay > 23h45m
```

**Why the agent's heartbeat:** it comes from the VM itself and only while it runs, independent of the
collector. **Why not suppressed:** this is exactly the off-hours situation the suppression window
would hide.

**Expected now and then:** if you start the VM yourself for maintenance outside business hours, this
fires. It's the reminder to deallocate when you're done.

**First checks:** GitHub Actions → VM schedule (did the 23:00 run fail?); deallocate: `vm-schedule.yml`
by hand, or `az vm deallocate -g rg-shoplite-prod -n prod-sql-01`.
