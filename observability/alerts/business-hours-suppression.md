# Business-hours suppression (alert processing rule)

The VM runs **Monday–Friday 13:00–23:00 UTC** and is deallocated the rest of the time. While it's off,
many rules would fire on purpose (no heartbeat, `/health` failing, backups "late"). This **alert
processing rule** removes the email from alerts fired outside business hours, without changing the
rules themselves. The alerts are still recorded in Azure (Monitor → Alerts), just not sent.

**Severity decides:** it suppresses **severities 2, 3, and 4**. The two always-on rules use
**severity 1**, so they reach you at any hour (`always-vm-running-off-hours`, `always-daily-cap`).
(Filtering by rule name "does not contain" isn't documented by Microsoft; filtering by severity is.)

Portal: **Monitor** → **Alerts** → **Alert processing rules** → **Create**.

| Tab | Field | Value |
|---|---|---|
| Scope | Scope | the resource group `rg-shoplite-prod` |
| Scope | Filter | **Severity** · Equals · **Sev2, Sev3, Sev4** |
| Rule settings | Rule type | **Suppress notifications** |
| Scheduling | Apply the rule | **Recurring** · time zone **(UTC) Coordinated Universal Time** |
| Scheduling | Recurrence 1 | **Daily**, from **22:55** to **13:30** (overnight, every night) |
| Scheduling | Recurrence 2 | **Weekly**, **Saturday** and **Sunday**, all day |
| Details | Name | `apr-shoplite-outside-business-hours` |
| Details | Description | Suppress Sev2-4 notifications while the VM is off: daily 22:55-13:30 UTC and weekends |
| Tags | | as above |

**Why these times:**
- **22:55:** the VM deallocates at 23:00; five minutes before, so its shutdown doesn't email you.
- **13:30:** the VM starts at 13:00 and the services need a few minutes; the first log backup runs at
  13:15 and needs to appear in a collector snapshot before the backup-age rule checks.
- **The weekend:** the daily window covers Friday night to Saturday 13:30 and Sunday night to Monday
  13:30; the weekly one covers Saturday and Sunday in between.

**Good to know:** changes take up to 30 minutes to apply. A suppressed alert sends nothing later, even
when the window ends. An alert that is still firing at 13:30 doesn't re-send by itself; the stateful
rules send when a new problem starts.
