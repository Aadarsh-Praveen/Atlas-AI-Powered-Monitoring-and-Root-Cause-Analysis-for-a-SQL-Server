# Vendor feed stale or unreachable

| Setting | Value |
|---|---|
| Rule name | `shoplite-ops-vendor-feed-stale` |
| Type | Log search alert on `law-shoplite-prod` |
| Severity | **3** (suppressed outside business hours) |
| Frequency / lookback | every 15 minutes / 1 hour |
| Condition | Table rows, greater than 0 |

**Meaning:** the collector's vendor check (every 15 minutes) found the vendor's newest price change
older than the data contract's 36 hours, or couldn't reach the vendor for a whole hour. The next ETL
run will fail.

```kusto
VendorFeed_CL
| where TimeGenerated > ago(1h)
| summarize Checks = count(), Unreachable = countif(Reachable == false),
            arg_max(TimeGenerated, FreshnessHours, Error)
| where (Checks >= 3 and Unreachable == Checks) or FreshnessHours > 36
```

**Why "a whole hour" for unreachable:** Neon's free tier sleeps and wakes; one failed check is a blip.
Three or more failed checks in a row is an outage.

**First checks:** `Error` for unreachable (Neon status, the URL in the collector's `.env`); for stale,
whether `vendor-day.yml` ran (GitHub Actions; disabled after 60 days of inactivity?) and the
`VENDOR_ACTIVITY_ENABLED` variable.
