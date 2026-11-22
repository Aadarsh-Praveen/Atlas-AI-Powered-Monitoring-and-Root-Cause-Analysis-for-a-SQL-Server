# API p95 latency above normal

| Setting | Value |
|---|---|
| Rule name | `shoplite-api-latency` |
| Type | Log search alert on `law-shoplite-prod` (Application Insights data, API 1.2.0+) |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 15 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** the slowest 5% of API requests in the last 15 minutes took more than 1.5 seconds.

```kusto
AppRequests
| where TimeGenerated > ago(15m)
| summarize Requests = sum(ItemCount), P95Ms = percentile(DurationMs, 95)
| where Requests >= 30 and P95Ms > 1500
```

**Why p95 and 1.5 s:** an average hides the slow requests shoppers notice; the 95th percentile shows
them. Most ShopLite requests take tens of milliseconds; checkout includes the simulated payment (up to
0.8 s). 1.5 s at p95 means something is really slow. After a week, look at the real normal and adjust:

```kusto
AppRequests
| where TimeGenerated > ago(7d)
| summarize P95Ms = percentile(DurationMs, 95) by bin(TimeGenerated, 1h)
| render timechart
```

**First checks:** p95 by `Name` (one endpoint or all?), the slow requests' `AppDependencies` (a slow SQL
call?), then the database side: `SqlTopQueries_CL` (`PlansForQuery > 1`: a plan change?), waits, blocking.
