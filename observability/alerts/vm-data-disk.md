# Data disk over 80% full

| Setting | Value |
|---|---|
| Rule name | `shoplite-vm-data-disk` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 15 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** `F:` (SQL data and logs, Extended Events files, app logs, the collector's buffer) has less
than 20% free. Full means SQL Server can't grow its files: writes fail.

```kusto
Perf
| where TimeGenerated > ago(15m)
| where ObjectName == "LogicalDisk" and CounterName == "% Free Space" and InstanceName == "F:"
| summarize FreePercent = avg(CounterValue), Samples = count()
| where Samples > 0 and FreePercent < 20
```

(From the Azure Monitor Agent's performance counters, `dcr-shoplite-vm`.)

**First checks:** on the VM, the biggest folders on `F:`: SQL data and log files (`SqlDatabases_CL`
sizes over time), `F:\collector\buffer` (a destination unreachable for days), `F:\XE` (capped at 500
MB), `F:\Logs`. The data disk can be enlarged (infra README), never shrunk.
