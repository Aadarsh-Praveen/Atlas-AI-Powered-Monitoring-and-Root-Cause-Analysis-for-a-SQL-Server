# No synthetic shopper traffic for 2 hours

| Setting | Value |
|---|---|
| Rule name | `shoplite-traffic-shoppers-missing` |
| Type | Log search alert on `law-shoplite-prod` (Application Insights data, API 1.2.0+) |
| Severity | **3** (suppressed outside business hours) |
| Frequency / lookback | every **1 hour** / **2 hours** (set **Override query time range** to 2 hours) |
| Condition | Table rows, greater than 0 |

**Meaning:** on a weekday between 15:00 and 23:00 UTC, the API received **no requests from the
Playwright shoppers** (`X-Synthetic-Source: playwright/<persona>`) in the last 2 hours. The shoppers
run every 20 minutes from 13:10 to 22:50, so 2 hours without one means about six runs in a row didn't
happen or didn't reach the API.

**Why it exists:** on Monday 2026-11-23 GitHub's scheduler ran **one** shoppers run all day, and
nothing told us: every workflow that didn't start is simply absent, and the ones that start check
`/health` and skip quietly. This rule makes a silent scheduler gap visible the **same day**.

```kusto
let shopperRequests = toscalar(
    AppRequests
    | where TimeGenerated > ago(2h)
    | where tostring(Properties["shoplite.synthetic_source"]) startswith "playwright/"
    | summarize sum(ItemCount));
print ShopperRequests = coalesce(shopperRequests, 0),
      Weekday = toint(dayofweek(now()) / 1d),       // 0 = Sunday ... 6 = Saturday
      HourUtc = hourofday(now())
| where ShopperRequests == 0
| where Weekday between (1 .. 5)                    // Monday to Friday
| where HourUtc between (15 .. 22)                  // only when the whole 2-hour window is in shopper hours
```

**Why it's written this way:**
- **"Missing" pattern:** `print` always returns one row, so the rule can fire on the *absence* of data
  (a plain `AppRequests | where ...` would return nothing, and nothing never fires). `coalesce` turns
  "no rows at all" into 0.
- **From 15:00 UTC only.** The suppression rule ends at 13:30, but the first shoppers start at 13:10:
  at 13:30 or 14:00 a 2-hour lookback still reaches back to before the VM started, and the rule would
  fire every morning. From 15:00 the whole window (13:00–15:00 and later) is shopper time.
- **`sum(ItemCount)`:** Application Insights keeps a 25% sample; each stored row stands for `ItemCount`
  requests. Six shopper runs make hundreds of API requests, so a sampled-away gap isn't realistic.
- **Shoppers only, not Locust:** the question is "did the scheduled shoppers run?"; Locust (`locust/`)
  and real people don't count. (One working source would otherwise hide another that stopped.)
- **Every hour is enough:** this is about a lost afternoon, not minutes. It's the cheapest frequency
  tier.

**Expected now and then (not a fault):**
- `TRAFFIC_ENABLED=false` during business hours (maintenance, game days): disable the rule meanwhile,
  or accept one email; the runbook's pausing steps mention it.
- The VM or the API is down: the shoppers check `/health` and skip. `api-health` and the collector
  heartbeat fire too, and they're the ones to follow.

**First checks:**
1. `TRAFFIC_ENABLED` (repository variables): `false`?
2. **Actions → Shoppers**: are runs **missing** for the last 2 hours (GitHub's scheduler: check
   https://www.githubstatus.com and the Monday-2026-11-23 note in `.github/workflows/README.md`), or
   present but **skipped** ("API not healthy": then it's the VM or the API, runbook section 7), or
   **red** (a broken page: the run's artifacts)?
3. If runs are missing, start one by hand (**Run workflow**) to keep some traffic going, and check the
   other scheduled workflows (load, restock) for the same gap.
