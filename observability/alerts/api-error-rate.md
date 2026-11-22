# API error rate above normal

| Setting | Value |
|---|---|
| Rule name | `shoplite-api-error-rate` |
| Type | Log search alert on `law-shoplite-prod` (Application Insights data, API 1.2.0+) |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 15 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** more than 5% of the API's requests in the last 15 minutes failed with a server error (5xx).

```kusto
AppRequests
| where TimeGenerated > ago(15m)
| summarize Requests = sum(ItemCount), ServerErrors = sumif(ItemCount, ResultCode startswith "5")
| extend ErrorPercent = round(100.0 * ServerErrors / Requests, 1)
| where Requests >= 30 and ErrorPercent > 5
```

**Why these numbers:**
- **5xx only.** A `409` (out of stock) is a normal answer, not a fault; Application Insights counts it
  as "failed" (`Success == false`), so the rule looks at the status code instead.
- **`sum(ItemCount)`, not `count()`.** With sampling (B4), each stored request stands for `ItemCount`
  requests; summing them gives the real totals.
- **At least 30 requests.** In a quiet 15 minutes, one error out of 10 requests is 10% and means
  nothing. "Above normal" here is a fixed threshold: dynamic thresholds cost extra; tune after a week.

**First checks:** `AppRequests` failures grouped by `Name` and `ResultCode`, then their
`AppExceptions` and `AppDependencies` (SQL calls) by `OperationId`; the API log; `/health`.
