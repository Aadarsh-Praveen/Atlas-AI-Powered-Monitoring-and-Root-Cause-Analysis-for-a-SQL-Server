# .github/workflows

GitHub Actions workflows: ShopLite's business-hours schedule and simulated traffic (Phase 3), the
vendor's daily changes (Phase 4), and later CI/CD and evaluation gates (Phase 10). SQL Server's own
jobs (backups, maintenance, the ETL) run in SQL Agent on the VM, not here; the combined timetable of
both is `server/jobs/schedule.md`. This README sits in `workflows/` rather than `.github/`
because GitHub would show a `.github/README.md` on the repo's front page instead of the root README.

> Claude Code writes these workflows but never runs or triggers them. You run them from the
> repository's **Actions** tab.

## The workflows

| Workflow | What it does | Obeys the kill switch |
|---|---|---|
| `vm-schedule.yml` | Starts `prod-sql-01` in the morning, deallocates it in the evening (OIDC sign-in to Azure, no stored secret) | **No**: the VM keeps its hours while traffic is paused |
| `shoppers.yml` | Playwright shoppers against the storefront (`shoplite/traffic/shoppers/`) | Yes |
| `load.yml` | Locust background load on the API, following the daily curve (`shoplite/traffic/load/`) | Yes |
| `restock.yml` | Nightly "receive a shipment": tops up low stock through `POST /api/admin/restock` | Yes |
| `vendor-day.yml` | Phase 4: one "vendor day" of changes in the vendor's Postgres (prices, stock, new and discontinued items; `vendor/activity.py`). Doesn't touch the VM. | **No**: it has its own switch, `VENDOR_ACTIVITY_ENABLED` (below) |

## Schedules

Cron schedules are always **UTC**. Boston is UTC−4 in summer (EDT, March to November) and UTC−5 in
winter (EST), so every local time below moves an hour twice a year.

| When (UTC, Mon–Fri) | Boston, summer (EDT) | Boston, winter (EST) | What |
|---|---|---|---|
| 12:00 (**every day**) | 8:00 AM | 7:00 AM | `vendor-day`: the vendor's daily changes (weekends included: the vendor keeps working while ShopLite's VM is off) |
| 13:00 | 9:00 AM | 8:00 AM | `vm-schedule`: **start** the VM |
| :10, :30, :50 from 13:10 to 22:50 | 9:10 AM–6:50 PM | 8:10 AM–5:50 PM | `shoppers`: 2 shoppers for 5 minutes |
| :35 from 13:35 to 22:35 | 9:35 AM–6:35 PM | 8:35 AM–5:35 PM | `load`: a 10-minute Locust session |
| Fridays 16:00 and 17:00 | 12:00 and 1:00 PM | 11:00 AM and 12:00 PM | `load`: the session that lands at 12:00 Boston hits the **flash sale** (12:00–12:20) |
| 22:30 | 6:30 PM | 5:30 PM | `restock`: receive a shipment (Phase 4 moved it from 22:45, so it lands before SQL Server's 22:45 and final 22:50 log backups even when GitHub starts it late) |
| 23:00 | 7:00 PM | 6:00 PM | `vm-schedule`: **deallocate** the VM |
| 04:00 (every day) | 12:00 AM | 11:00 PM | Not a workflow: the VM's own Azure auto-shutdown, a safety net if the 23:00 run fails |

Weekends: the VM stays off and no traffic runs; only `vendor-day` runs.

**GitHub's scheduled runs can start several minutes late**, and occasionally a run is skipped when
GitHub is busy. That's fine for synthetic traffic. Every traffic workflow checks the API's `/health`
first, so a run that starts before the VM is up (or after it's stopped) just skips.

## The kill switch: `TRAFFIC_ENABLED`

A repository **variable** (Settings → Secrets and variables → Actions → **Variables**).

- `true`: shoppers, load, and restock run on their schedules.
- anything else (`false`, or not set): those three show as **skipped** on every run. Use this during
  maintenance, deployments, and incident experiments.

It doesn't affect `vm-schedule.yml`. To keep the VM off as well, disable that workflow (below).

## The vendor's switch: `VENDOR_ACTIVITY_ENABLED`

Also a repository **variable**, for `vendor-day.yml` only. The vendor is an external system, so
ShopLite's `TRAFFIC_ENABLED` doesn't pause it.

- not set, or anything other than `false`: the vendor changes every day (the default).
- `false`: the vendor's data stays still, e.g. while testing the ETL against a fixed feed.

Off for more than about a day and a half, and the ETL refuses the feed as stale (the data contract's
36-hour freshness rule): a realistic "the supplier stopped updating" failure.

## Secrets and variables

Set them in **Settings → Secrets and variables → Actions**. The OIDC values are created in the OIDC
setup guide (Phase 3, B5).

| Name | Type | Used by | Value |
|---|---|---|---|
| `AZURE_CLIENT_ID` | Secret | `vm-schedule` | The managed identity's **client ID** |
| `AZURE_TENANT_ID` | Secret | `vm-schedule` | Your Entra tenant ID (Default Directory) |
| `AZURE_SUBSCRIPTION_ID` | Secret | `vm-schedule` | The subscription that holds `rg-shoplite-prod` |
| `SHOPLITE_ADMIN_TOKEN` | Secret | `shoppers` (admin persona), `load` (AdminUser), `restock` | The **production** `ADMIN_TOKEN` from `C:\atlas\shoplite\api\.env` |
| `VENDOR_ACTIVITY_URL` | Secret | `vendor-day` | The vendor Postgres URL as the **`vendor_activity`** role (never the admin), ending `?sslmode=require` (`vendor/README.md`) |
| `TRAFFIC_ENABLED` | Variable | `shoppers`, `load`, `restock` | `true` / `false` |
| `VENDOR_ACTIVITY_ENABLED` | Variable (optional) | `vendor-day` | `false` pauses the vendor; not set = on |
| `STOREFRONT_URL` | Variable | `shoppers` | `https://shoplite-aadarsh.vercel.app` |
| `API_URL` | Variable | all | `https://shoplite-aadarsh.northcentralus.cloudapp.azure.com` |
| `SHOPPERS_CONCURRENCY` | Variable (optional) | `shoppers` | Default `2` (max 8) |
| `SHOPPERS_DURATION_SECONDS` | Variable (optional) | `shoppers` | Default `300` |
| `LOAD_PEAK_USERS` | Variable (optional) | `load` | Default `5`. Raise it slowly while watching CPU credits. |

GitHub masks secrets in logs, and workflows triggered from forks don't receive them. The workflows
never print a secret: the admin token is passed only to the step that uses it, and sent only as a
request header.

## Running a workflow by hand

**Actions** tab → choose the workflow on the left → **Run workflow** (top right) → fill in the
inputs → **Run workflow**.

| Workflow | Inputs |
|---|---|
| `vm-schedule` | `action`: `start` or `deallocate` |
| `shoppers` | `duration` (seconds, default 300), `concurrency` (default 2) |
| `load` | `duration` (seconds, default 300), `peak_users` (default 5) |
| `restock` | `target_quantity` (default 100), `max_products` (default 500) |
| `vendor-day` | `scale` (default 1; 2 = a busier vendor day) |

Manual runs still respect the kill switch and the health check (except `vm-schedule`, and
`vendor-day`, which follows `VENDOR_ACTIVITY_ENABLED` instead).

## Reading the results

Open a run from the Actions tab:
- **Summary** (the run's front page): `shoppers` shows outcome and persona tables plus the API status
  codes; `load` shows the session's settings and Locust's exit code; `vendor-day` shows the changes per kind; `restock` shows products restocked
  and units added; `vm-schedule` shows the VM's power state and how long the API took to answer.
- **Artifacts** (bottom of the summary): `shoppers-failure-<id>` (screenshots and traces of broken
  pages, **only when a page broke**, kept 5 days) and `locust-report-<id>` (the HTML report, kept 7
  days). View shopper traces at https://trace.playwright.dev (see `shoplite/traffic/shoppers/README.md`).
- **A red run** means: a shopper found a broken page, Locust counted failures (503s or unexpected
  errors), the restock call failed, or Azure refused the VM action. A **skipped** run means the kill
  switch is off; a green run whose summary says "skipped" means the API wasn't healthy.

## Scheduled workflows stop after 60 days without activity

**GitHub disables scheduled workflows in a public repository when the repository has had no activity
for 60 days** (no commits, roughly). It emails a warning beforehand. Manual runs still work, but
nothing runs on its schedule (no VM start or stop, no traffic, no restock) until you switch them back
on.

To re-enable them: **Actions** tab → select the workflow on the left → the banner says it was
disabled → **Enable workflow**. Do it for each of `vm-schedule`, `shoppers`, `load`, `restock`, and
`vendor-day`. (A disabled `vendor-day` means the vendor's prices go stale, and about a day and a half later the ETL
starts refusing the feed.)
(With the GitHub CLI: `gh workflow enable vm-schedule.yml`, and so on.)

To **prevent** it: any push (a commit) before the 60 days are up counts as activity and resets the
clock. Once GitHub has disabled the workflows, though, a push doesn't turn them back on; enable them
as above.

The same page has **Disable workflow** under the "…" menu, the cleanest way to stop the VM schedule on
purpose, for example during a long break. The VM's 04:00 UTC auto-shutdown still applies.
