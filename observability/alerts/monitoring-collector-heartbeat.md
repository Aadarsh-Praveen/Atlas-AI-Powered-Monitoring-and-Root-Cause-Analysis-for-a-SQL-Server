# No collector heartbeat for 5 minutes

| Setting | Value |
|---|---|
| Rule name | `shoplite-monitoring-collector-heartbeat` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours: at night the VM, and so the collector, is off on purpose) |
| Frequency / lookback | every 5 minutes / 30 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** the collector sends a heartbeat every minute; none has arrived for 5 minutes. Either the
collector (or the VM) stopped, or it can't send to Log Analytics. Until it's back, the SQL Server and
operations alerts are blind.

```kusto
let last = toscalar(CollectorHeartbeat_CL | where TimeGenerated > ago(30m) | summarize max(TimeGenerated));
print LastHeartbeat = last, MinutesSince = datetime_diff("minute", now(), last)
| where isnull(LastHeartbeat) or LastHeartbeat < ago(5m)
```

**Why `print` and `toscalar`:** a rule that counts rows can't fire on "no rows". This query always
produces one row with the newest heartbeat time, and keeps it only when that's missing or too old.

**Note:** Log Analytics usually shows data within 1–3 minutes, occasionally longer. If this fires now
and then with the collector fine, raise `ago(5m)` to `ago(10m)`.

**First checks:** on the VM, `Get-Service shoplite-collector` and its log (`F:\Logs\collector`: `send
failed` lines?); the agent's `Heartbeat` table (is the VM itself alive?).
