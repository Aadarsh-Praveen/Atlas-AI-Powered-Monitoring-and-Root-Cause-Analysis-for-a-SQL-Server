# ShopLite production schedule

Everything that runs on its own, in one place: SQL Agent jobs on prod-sql-01 (`server/jobs/sql/040_jobs.sql`),
GitHub Actions workflows (`.github/workflows/`), and the two Azure Logic Apps that start and stop the
VM (`infra/vm-schedule-logic-apps.md`). All schedules are defined in **UTC**; Boston is
UTC−4 in summer (EDT, March to November) and UTC−5 in winter (EST).

The VM runs **13:00–23:00 UTC, Monday to Friday**. Nothing is scheduled outside that window except the
vendor (which doesn't need the VM) and the safety-net shutdown. A SQL Agent job scheduled while the VM
is off simply never runs, with no error anywhere, so none is.

| UTC | Boston summer | Boston winter | Days | What | Where |
|---|---|---|---|---|---|
| 12:00 | 8:00 AM | 7:00 AM | **every day** | Vendor day: prices, stock, catalog change | GitHub `vendor-day.yml` (own switch `VENDOR_ACTIVITY_ENABLED`) |
| 13:00 | 9:00 AM | 8:00 AM | Mon–Fri | **VM starts**; SQL Agent and the XE session start with it | Azure Logic App `logic-shoplite-vm-start` (temporarily also GitHub `vm-schedule.yml`, until the Logic Apps are verified) |
| :10, :30, :50, 13:10–22:50 | 9:10 AM–6:50 PM | 8:10 AM–5:50 PM | Mon–Fri | Playwright shoppers, 5 minutes | GitHub `shoppers.yml` |
| every 15 min, 13:15–22:45 | 9:15 AM–6:45 PM | 8:15 AM–5:45 PM | Mon–Fri | SalesDB **log backup** | Agent: Backup SalesDB LOG |
| :35, 13:35–22:35 | 9:35 AM–6:35 PM | 8:35 AM–5:35 PM | Mon–Fri | Locust load, 10 minutes | GitHub `load.yml` |
| 14:00 | 10:00 AM | 9:00 AM | Mon–Fri | **Vendor ETL** into SalesDB | Agent: Vendor ETL |
| 14:30 | 10:30 AM | 9:30 AM | Mon–Fri | ReportingDB incremental refresh | Agent: ReportingDB refresh |
| 16:00 and 17:00 | 12:00 and 1:00 PM | 11:00 AM and 12:00 PM | Fri | Locust sessions; the one at 12:00 Boston is the **flash sale** | GitHub `load.yml` |
| 17:00 | 1:00 PM | 12:00 PM | Mon–Fri | SalesDB **differential** backup | Agent: Backup SalesDB DIFF |
| 20:00 | 4:00 PM | 3:00 PM | **Wed** | **Restore test** (full + diff + logs → CHECKDB) | Agent: Restore test |
| 20:30 | 4:30 PM | 3:30 PM | **Fri** | ReportingDB **full** rebuild | Agent: ReportingDB full refresh |
| 21:00 | 5:00 PM | 4:00 PM | **Thu** | **Integrity check** (DBCC CHECKDB) | Agent: Integrity check |
| 21:30 | 5:30 PM | 4:30 PM | Mon–Fri | SalesDB **full** backup | Agent: Backup SalesDB FULL |
| 21:45 | 5:45 PM | 4:45 PM | **Fri** | Full backup of system databases, ReportingDB, DBA | Agent: Backup system, ReportingDB, DBA FULL |
| 22:00 | 6:00 PM | 5:00 PM | Mon–Fri | Index and statistics maintenance (starts nothing new after 30 min) | Agent: Index and statistics maintenance |
| 22:30 | 6:30 PM | 5:30 PM | Mon–Fri | Restock (receive a shipment) | GitHub `restock.yml` |
| 22:35 | 6:35 PM | 5:35 PM | Mon–Fri | Trim backup history, job history, CommandLog (30 days) | Agent: History cleanup |
| 22:50 | 6:50 PM | 5:50 PM | Mon–Fri | SalesDB **final log backup** of the day | Agent: Backup SalesDB LOG |
| 23:00 | 7:00 PM | 6:00 PM | Mon–Fri | **VM deallocates** | Azure Logic App `logic-shoplite-vm-deallocate` (temporarily also GitHub `vm-schedule.yml`) |
| 04:00 | 12:00 AM | 11:00 PM | every day | Safety-net shutdown if the 23:00 deallocate failed | Azure auto-shutdown |

## Why this order

- **The VM's hours are in Azure, not GitHub:** GitHub's scheduler can be hours late or skip runs (on
  2026-11-23 the 13:00 start arrived at 00:30 the next day). Traffic, restock, and the vendor stay on
  GitHub schedules: a late or missing run there costs some synthetic traffic, not a VM running all night
  or off all day.
- **Vendor 12:00 → ETL 14:00 → ReportingDB 14:30:** each step uses the previous one's output, with a
  buffer for a slow start (GitHub can start scheduled runs several minutes late).
- **Restock 22:30, then log backups 22:45 and 22:50:** the restock's changes are in Blob Storage before
  the VM stops. 22:30 leaves 15 minutes for GitHub being late.
- **The heavy jobs are spread over the evenings** so the burstable B2as_v2 doesn't spend all its CPU
  credits at once: restore test Wednesday, CHECKDB Thursday, ReportingDB full rebuild and the weekly
  backups Friday (after the flash sale). The daily full backup and index maintenance are light at
  SalesDB's size.
- **Recovery point:** about 15 minutes during business hours (log backups). Changes after 22:50 stay in
  the log on the data disk overnight and go into the next weekday's 13:15 log backup.

## Busiest evenings (watch CPU credits)

| Day | From 20:00 UTC |
|---|---|
| Wednesday | restore test 20:00, full backup 21:30, index maintenance 22:00 |
| Thursday | CHECKDB 21:00, full backup 21:30, index maintenance 22:00 |
| Friday | ReportingDB full rebuild 20:30, full backup 21:30, weekly backups 21:45, index maintenance 22:00 |

The day after a change to any of these, check the VM's **CPU Credits Remaining** metric in the portal
(runbook, "Is it healthy?").
