# Restore test missed

| Setting | Value |
|---|---|
| Rule name | `shoplite-sql-restore-test-missed` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 1 hour / **2 days** (the maximum a log search alert allows) |
| Condition | Table rows, greater than 0 |

**Meaning:** it's Thursday and no restore test has succeeded since Wednesday's 20:00 run: it failed
(the other rule fired too), never ran (job disabled, VM off that evening), or ran but wasn't collected.

The phase plan says "restore test older than 8 days". Log search alerts scan at most 2 days of data,
so the rule checks the same thing a different way: the test runs every Wednesday, so on **Thursday**
a success must exist within the last 2 days. A missed week is caught the next afternoon instead of
after 8 days.

```kusto
let lastSuccess = toscalar(DbaRestoreTests_CL
    | where TimeGenerated > ago(2d) and Status == "succeeded"
    | summarize max(TimeGenerated));
print LastSuccessWithin2Days = lastSuccess
| where dayofweek(now()) == 4d and isnull(LastSuccessWithin2Days)      // 4d = Thursday
```

It fires from 13:30 Thursday (when the suppression window ends) and resolves itself on Friday.

**First checks:** the "ShopLite - Restore test" job in `SqlAgentJobRuns_CL` (did it run?), then
`DBA.dbo.restore_test_runs` on the VM.
