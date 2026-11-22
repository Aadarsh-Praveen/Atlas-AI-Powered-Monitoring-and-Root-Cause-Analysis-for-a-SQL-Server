# Test alert (Phase 5, Part C3, step 4)

Proves the whole chain works end to end, the way a real incident would travel:

```
something goes wrong ─▶ data stops or changes ─▶ Log Analytics ─▶ alert rule fires ─▶ action group ─▶ your email
                                                                       └─ and resolves ─▶ "Resolved" email
```

The action group's own **Test** button (`alerts/action-group.md`) only proves the last step. This test
proves every step, including the stateful "resolved" email.

**When:** a weekday between **13:30 and 22:30 UTC**. Outside that, the business-hours suppression rule
holds back severity-2 emails, and the test would look like a failure.

## The test: stop the collector for 10 minutes

The collector heartbeat alert (`alerts/monitoring-collector-heartbeat.md`, every 5 minutes) fires when
no heartbeat has arrived for 5 minutes. Stopping the collector is harmless: shoppers aren't affected,
and the collector's watermarks mean the event tables catch up when it starts again. Only the minute-by-
minute snapshots (waits, sessions) have a gap, which is fine.

1. **Note the time** (UTC). On the VM, PowerShell as Administrator:

   ```powershell
   Stop-Service shoplite-collector
   ```

2. **Wait.** Within about 10–12 minutes (5 minutes without heartbeats, then up to 5 more until the
   rule's next run, plus a minute or two of Log Analytics delay), an email arrives from Azure Monitor:
   **Fired: Sev2 · shoplite-monitoring-collector-heartbeat**. In the portal, **Monitor** → **Alerts**
   shows it as **Fired**.

3. **Start it again:**

   ```powershell
   Start-Service shoplite-collector
   ```

4. **Wait for the resolution.** The rule is stateful: once heartbeats are back, it resolves after three
   clean evaluations (about 15 minutes) and sends **Resolved: Sev2 · shoplite-monitoring-collector-heartbeat**.

5. **Record the result in the runbook (B7):** when you stopped it, when the email arrived, when the
   resolved email arrived. That's the alerting delay you can expect for a real problem.

| Didn't happen | Check |
|---|---|
| No "Fired" email after 15 minutes | Monitor → Alerts: does the alert show as Fired? If yes, the problem is the action group (spam folder, the email address in `ag-shoplite-email`) or the suppression rule (wrong time window or time zone). If no, the rule: open it, run its query by hand; check the rule is enabled and its scope is the workspace. |
| "Fired" arrives, "Resolved" never does | **Automatically resolve alerts** is off on the rule (Details tab): turn it on. |
| Fired immediately when you started the test | Heartbeats were already missing: check the collector was healthy before (`CollectorHeartbeat_CL | top 5 by TimeGenerated desc`). |

## Alternative: a temporary rule that is always true

If you'd rather not stop the collector, create a throwaway rule whose query always returns a row:

| Field | Value |
|---|---|
| Scope / signal | `law-shoplite-prod` / Custom log search |
| Query | `print Test = "This is a test alert from observability/test-alert.md"` |
| Condition | Table rows, greater than 0; every 5 minutes; lookback 5 minutes |
| Action group / severity / name | `ag-shoplite-email` / **2** / `shoplite-test-alert` |

It fires at its first evaluation and emails you. **Delete the rule straight afterwards** (Monitor →
Alerts → Alert rules → `shoplite-test-alert` → Delete): it keeps firing and costs money while it
exists. This proves rule → action group → email, but not "data stops → alert", which the collector
test does.

## The always-on path (severity 1)

The two severity-1 rules aren't suppressed, so they're worth seeing once too. The natural test comes for
free: the next time you start the VM by hand outside business hours (for maintenance, say),
`shoplite-always-vm-running-off-hours` should email you within about 15–30 minutes. Note it in the
runbook when it happens. Don't hit the daily cap on purpose: it stops all data for the rest of the day.
