# CPU credits low

| Setting | Value |
|---|---|
| Rule name | `shoplite-vm-cpu-credits` |
| Type | **Metric alert** on the VM `prod-sql-01` (platform metric, free to collect) |
| Signal | **CPU Credits Remaining** |
| Condition | Static, aggregation **Average**, operator **Less than**, threshold **30** |
| Aggregation granularity / frequency | 15 minutes / every 5 minutes |
| Severity | **2** (suppressed outside business hours) |
| Automatically resolve | Yes (metric alerts do by default) |
| Action group | `ag-shoplite-email` |

**Meaning:** the B2as_v2 is a burstable VM: it runs at full speed while it has CPU credits and drops to
a low baseline when they run out. Under 30 credits, it's close to being throttled: slow pages and slow
queries with nothing else visibly wrong.

**Why 30:** a credit is one minute of one full vCPU above the baseline, so 30 credits is about 15
minutes of both cores flat out: time to notice and react. Adjust after a week of watching the metric
(normal days should stay far above it).

**Portal:** VM `prod-sql-01` → **Alerts** → **Create** → **Alert rule** → signal **CPU Credits
Remaining** → the values above → Actions `ag-shoplite-email` → Details: name, severity 2.

**First checks:** VM → Metrics → CPU Credits Remaining and Percentage CPU over the day; what was busy
(`SqlTopQueries_CL` by `TotalCpuMs`, `Perf` for `sqlservr` and `python`, the Locust schedule).
