# Deadlocks

| Setting | Value |
|---|---|
| Rule name | `shoplite-sql-deadlocks` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 20 minutes |
| Condition | Table rows, greater than 0 |

**Meaning:** two (or more) sessions each held a lock the other needed; SQL Server killed one of them
(the "victim", error 1205). A shopper's request failed.

```kusto
SqlXEvents_CL
| where TimeGenerated > ago(20m) and EventName == "xml_deadlock_report"
| summarize Deadlocks = dcount(EventId), Latest = max(TimeGenerated)
| where Deadlocks > 0
```

**Why any deadlock:** ShopLite's normal workload shouldn't deadlock at all; one is worth a look, and a
burst means a code or index problem. (`dcount(EventId)` ignores duplicate deliveries.)

**First checks:** the latest `xml_deadlock_report` rows' `EventXml`: the deadlock graph names both
sessions, their statements, the objects and lock modes. Same objects every time = a pattern to fix.
