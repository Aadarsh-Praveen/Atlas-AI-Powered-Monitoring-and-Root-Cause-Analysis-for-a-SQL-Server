# Blocking over 30 seconds

| Setting | Value |
|---|---|
| Rule name | `shoplite-sql-blocking` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **2** (suppressed outside business hours) |
| Frequency / lookback | every 5 minutes / 10 minutes |
| Condition | Table rows, greater than 0 (README, "Common settings") |

**Meaning:** a session has waited more than 30 seconds for a lock that another session holds. Shoppers
queue behind it: slow pages, then timeouts.

```kusto
SqlBlocking_CL
| where TimeGenerated > ago(10m)
| where Role == "blocked" and WaitSeconds > 30
| summarize BlockedSessions = dcount(SessionId), LongestWaitSeconds = max(WaitSeconds),
            HeadBlockers = make_set(HeadBlocker, 10), Programs = make_set(ProgramName, 10)
| where BlockedSessions > 0
```

**Why 30 seconds:** short blocking (milliseconds to a few seconds) is normal under load. 30 seconds is
long enough to be a real problem and short enough to catch it while it's happening. The XE session
reports blocking at 10 seconds, so the evidence is already there when this fires.

**First checks:** the `head_blocker` rows in `SqlBlocking_CL` for the same minutes: `ProgramName`,
`LoginName`, `SessionStatus`, `TransactionSeconds`, `StatementText`. A **sleeping** head blocker with an
old open transaction is the classic "someone forgot to commit". Then the `blocked_process_report`
events in `SqlXEvents_CL`.
