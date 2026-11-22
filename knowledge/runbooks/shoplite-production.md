# Runbook: ShopLite production

How ShopLite runs in production, how to operate it day to day, and what to check when something looks
wrong. Written in Phase 2, extended in Phase 4 (jobs, backups and restores, the vendor feed, ReportingDB,
Extended Events); the first document in `knowledge/`. Atlas reads it from Phase 7 onward.

**Last verified against the real setup:** _(fill in the date after Phase 2, Part C6)_ · Phase 4 sections (13–17): _(fill in after Phase 4, Part C4)_

---

## 1. What runs where

```
Shopper's browser ──HTTPS──▶ Vercel: ShopLite storefront, https://shoplite-aadarsh.vercel.app
        │
        └── API calls, HTTPS ──▶ shoplite-aadarsh.northcentralus.cloudapp.azure.com  (static public IP)
                                    Azure VM prod-sql-01
                                    ├─ Caddy           ports 443 and 80     service "caddy"
                                    ├─ ShopLite API    127.0.0.1:8000       service "shoplite-api"
                                    ├─ SQL Server 2022 Developer, localhost:1433
                                    │    SalesDB, ReportingDB, DBA          service "MSSQLSERVER"
                                    └─ SQL Server Agent (jobs)              service "SQLSERVERAGENT"
                                         ├── BACKUP TO URL ──HTTPS──▶ Azure Storage, container "sqlbackups"
                                         └── vendor ETL ──TLS, read-only──▶ Vendor Postgres (Neon, US East)

GitHub Actions ──▶ vendor-day.yml ──TLS──▶ Vendor Postgres (the vendor's daily changes)
```

The storefront never talks to SQL Server. Every click in the browser becomes a request to the API,
and the API queries SalesDB. Since Phase 4, SQL Agent on the same VM runs the backups, maintenance,
the vendor ETL, and the ReportingDB refreshes (sections 13–17).

| Component | Where | Details |
|---|---|---|
| Storefront | Vercel | **https://shoplite-aadarsh.vercel.app** (the production domain; Vercel's longer deployment domain redirects to it). Built from GitHub, root directory `shoplite/web`. Calls the API at `NEXT_PUBLIC_API_URL` = `https://shoplite-aadarsh.northcentralus.cloudapp.azure.com`, a Vercel **Config** variable: public on purpose, since it's copied into the JavaScript every browser downloads, and fixed at build time (changing it needs a redeploy). |
| VM | Azure, resource group `rg-shoplite-prod` | `prod-sql-01`, Standard_B2as_v2 (2 vCPU AMD, 8 GB RAM, burstable, no temporary disk), Windows Server 2022, time zone **UTC**. Region North Central US (`northcentralus`). Tags `project=atlas`, `env=prod`, `app=shoplite`. Defined in `infra/` (Bicep). |
| Caddy | VM, service `caddy` | HTTPS front door. Let's Encrypt certificate for the DNS name, renewed automatically. Redirects HTTP to HTTPS and forwards to `127.0.0.1:8000`. Config: `C:\atlas\shoplite\api\deploy\Caddyfile`. |
| ShopLite API | VM, service `shoplite-api` | FastAPI on Uvicorn, **one process** (no extra workers; see section 8), listening on **127.0.0.1:8000 only**. Runs as `NT AUTHORITY\LocalService`. Code in `C:\atlas`; settings in `C:\atlas\shoplite\api\.env`. Restart it with `C:\atlas\shoplite\api\deploy\restart-api.ps1`. |
| SQL Server | VM, service `MSSQLSERVER` | SQL Server 2022 Developer. Accepts local connections only. Max server memory 5 GB. Databases: **`SalesDB`** (about 1,000,000 orders; **FULL** recovery since Phase 4, log backups every 15 minutes), **`ReportingDB`** (daily sales summaries rebuilt from SalesDB; SIMPLE), **`DBA`** (Ola Hallengren's maintenance procedures, `CommandLog`, restore test results; SIMPLE). Extended Events session `ShopLite_Diagnostics` (section 17). |
| SQL Server Agent | VM, service `SQLSERVERAGENT` | Starts automatically with the VM. Runs every database job (section 13). Its account `NT SERVICE\SQLSERVERAGENT` runs the ETL and can read `C:\atlas\server\etl\.env`. |
| Backup storage | Azure, `rg-shoplite-prod` | Storage account `stshoplite…` (Standard LRS), private container **`sqlbackups`**, lifecycle rule deleting backups after **14 days**. SQL Server writes to it with a SAS credential (section 14). Defined in `infra/modules/storage.bicep`. |
| Vendor database | Neon (free managed Postgres), US East | "The vendor": an outside supplier network's Postgres, schema `supply`. ShopLite reads it with the read-only `shoplite_reader` login (TLS required); it changes every day through `vendor-day.yml`. Not in Azure, and not ShopLite's to change (section 15). |

## 2. Ports

| Port | Open to | Purpose | Enforced by |
|---|---|---|---|
| 443 | Internet | HTTPS to Caddy, which forwards to the API | NSG `Allow-HTTPS-Inbound` (110), Windows Firewall rule "ShopLite - HTTPS in (443)" |
| 80 | Internet | Let's Encrypt challenges; everything else redirects to 443 | NSG `Allow-HTTP-Inbound` (120), Windows Firewall rule "ShopLite - HTTP in (80)" |
| 3389 | The owner's IP only | Remote Desktop | NSG `Allow-RDP-From-Owner` (130) |
| 1433 | **Nobody outside the VM** | SQL Server | NSG `Deny-SQL-Inbound` (**100**, checked first), SQL connectivity LOCAL, Windows Firewall block rule "ShopLite - Block SQL from outside (1433)" |
| 8000 | **Nobody outside the VM** | The API, for Caddy only | Uvicorn binds to 127.0.0.1; no NSG or firewall rule allows it |
| 2019 | Nobody outside the VM | Caddy's admin API | Bound to localhost by default |

## 3. Logins and secrets

Secret **values** are in the owner's password manager, never in Git or in this document.

| Login | Used by | Notes |
|---|---|---|
| Windows admin (the `adminUsername` chosen in Bicep) | The owner, over RDP | SQL sysadmin through Windows authentication on this image. |
| SQL admin (`sqlAdminUsername`, not `sa`) | `apply_schema.py`, `seed.py` | Created by the SQL IaaS extension. In the VM's root `.env` (`C:\atlas\.env`) as `SQL_ADMIN_USER` / `SA_PASSWORD`. |
| `shoplite_app` | The API | Least privilege in SalesDB: read/write data and EXECUTE only. `DB_PASSWORD` (API `.env`) **must be identical** to `SHOPLITE_APP_PASSWORD` (root `.env`), the value `apply_schema.py` created the login with. A mismatch: every request fails, `/health` returns 503, and the API log and SQL Server's `ERRORLOG` show `Login failed for user 'shoplite_app'. (18456)`. |
| Admin token | The storefront's admin pages | `ADMIN_TOKEN` in the API `.env`. Not real authentication. |
| `shoplite_etl` (Phase 4) | The vendor ETL | Least privilege in SalesDB: insert into the staging table, run the two `vendor` procedures, read and write `ops.etl_runs`. Nothing in `dbo`. Password in `C:\atlas\server\etl\.env` (`SALESDB_PASSWORD`), identical to the value given to `server/etl/sql/002_etl_login.sql`. |
| `shoplite_reader` (vendor Postgres) | The vendor ETL | Read-only on the vendor's four `supply` tables. Inside `VENDOR_READER_URL` in the ETL `.env`. The ETL refuses any other login. |
| `vendor_activity` (vendor Postgres) | `vendor-day.yml` | Writes vendor data only; no delete, no structure changes. GitHub secret `VENDOR_ACTIVITY_URL`. The vendor admin connection never leaves the owner's Mac. |
| Backup SAS (Phase 4) | SQL Server's backups and restores | Stored only inside SQL Server, as the credential named after the container URL. Permissions read, write, create, list (no delete), HTTPS only. **Expires: _(fill in YYYY-MM-DD when created)_, signed with key _(1 or 2)_.** Rotate before it expires (section 10). |
| `reporting_reader` role | Nobody yet | Read-only on ReportingDB's reports. Grafana's and Atlas's logins join it later. |

The `.env` files on the VM are readable only by Administrators and SYSTEM, plus one service account
each, read-only: LocalService for the API's, `NT SERVICE\SQLSERVERAGENT` for the ETL's.

## 4. Starting and stopping

The VM keeps **business hours** (Phase 3), all times UTC, Monday to Friday:

| UTC | Boston, summer / winter | What | By |
|---|---|---|---|
| 13:00 | 9 AM / 8 AM | **Start** | `.github/workflows/vm-schedule.yml` (OIDC, managed identity) |
| 23:00 | 7 PM / 6 PM | **Deallocate** | `vm-schedule.yml` |
| 04:00 (daily) | 12 AM / 11 PM | **Safety-net shutdown** if the 23:00 run failed | Azure auto-shutdown `shutdown-computevm-prod-sql-01` (production's value; the Bicep default is 01:00) |

Inside these hours SQL Agent runs the database jobs (backups, the ETL, maintenance; Phase 4) and
GitHub runs the traffic and the restock. The full timetable of both, in UTC and Boston time, is
`server/jobs/schedule.md`. Anything scheduled while the VM is off never runs.

Weekends it stays off. To start or stop it outside these hours, run `vm-schedule.yml` by hand
(Actions → VM schedule → Run workflow), or from the Mac:

```bash
az vm start      -g rg-shoplite-prod -n prod-sql-01      # start (about 2-3 minutes until the site answers)
az vm deallocate -g rg-shoplite-prod -n prod-sql-01      # stop and release the compute (stops compute billing)
az vm get-instance-view -g rg-shoplite-prod -n prod-sql-01 \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].displayStatus" -o tsv
```

- **"VM deallocated"** is the state that stops compute charges. **"VM stopped"** (from `az vm stop` or
  shutting down Windows from inside) still bills for compute.
- **Always billing**, even when deallocated: the two managed disks (OS and 64 GB data) and the static
  public IP.
- On start, the services come up by themselves: SQL Server, then the API (it waits for SQL Server),
  and Caddy. No manual steps.

## 5. Is it healthy?

From anywhere:

```bash
curl -s https://shoplite-aadarsh.northcentralus.cloudapp.azure.com/health
```

| Response | Meaning |
|---|---|
| `200`, `{"status":"ok","version":"x.y.z","database":"ok"}` | API up and SalesDB reachable. `version` is the deployed release. |
| `503`, `"status":"unavailable","database":"unreachable"` | The API runs but can't reach SQL Server: SQL Server down, connection pool exhausted, or the wrong password (`Login failed for user 'shoplite_app'. (18456)` in the API log means `DB_PASSWORD` doesn't match `SHOPLITE_APP_PASSWORD`). |
| `502` from Caddy | Caddy runs but the API doesn't answer on 127.0.0.1:8000 (service stopped, crashing, or starting). |
| Connection timeout or refused | The VM is off or deallocated, Caddy is stopped, or ports 443/80 are blocked. |
| Certificate error | Caddy couldn't get or renew the certificate (see section 7). |

On the VM (PowerShell as Administrator):

```powershell
Get-Service MSSQLSERVER, SQLSERVERAGENT, shoplite-api, caddy       # all four: Running
Invoke-RestMethod http://127.0.0.1:8000/health                       # bypasses Caddy
Get-Volume -DriveLetter C, F | Format-Table DriveLetter, SizeRemaining, Size
```

**CPU credits (burstable VM).** `prod-sql-01` is a B2as_v2: it runs at full speed while it has CPU
credits and drops to a low baseline (about 40% of its CPU) when they run out. In the portal, open the
VM → **Metrics** → **CPU Credits Remaining**. Near zero means the VM is throttled. You'll see slow
API responses and long-running queries with nothing else wrong: services running, `/health` ok,
plenty of disk. Sustained work drains credits: the seed, load tests, heavy reports. Credits refill
while the VM is quieter than its baseline.

In SQL Server, the API's connections are tagged with the release:

```sql
SELECT program_name, COUNT(*) AS sessions
FROM sys.dm_exec_sessions
WHERE program_name LIKE N'ShopLite-API/%'
GROUP BY program_name;
```

Expect **up to 15** API sessions: one process × (pool 10 + overflow 5). More than one `program_name`
at once means two releases are running. Outside the few seconds of a restart, that's an old API
process that survived; see section 8.

**The daily operations (Phase 4)**, once a day after 15:00 UTC:

| Check | Healthy | Where |
|---|---|---|
| Every ShopLite job's last run | `succeeded` | Section 13, "Last run of every job" |
| Today's ETL | `succeeded`, rows read about 10,000, `rows_skipped` 0 | Section 15 |
| Today's ReportingDB refresh | `succeeded` | Section 16 |
| Latest log backup | less than 20 minutes old (business hours) | Section 14, "Are the backups healthy?" |
| Latest restore test | `succeeded`, less than 8 days old | Section 14 |
| Backup SAS expiry (section 3) | more than 30 days away | the date in section 3 |

## 6. Logs

All logs are on the data disk and rotate, so they can't fill it.

| Log | Path | Contents |
|---|---|---|
| API | `F:\Logs\shoplite-api\shoplite-api.err.log` | One line per request (method, path, status, duration, request ID) and errors with tracebacks. Rotated at 10 MB, 10 kept. (`.out.log` is usually empty.) |
| Caddy access | `F:\Logs\caddy\access.log` | One JSON line per request reaching Caddy, with client IP and status. Rotated at 10 MB, 10 kept. |
| Caddy | `F:\Logs\caddy\caddy.log` | Startup, configuration, and certificate events. |
| SQL Server | `C:\Program Files\Microsoft SQL Server\MSSQL16.MSSQLSERVER\MSSQL\Log\ERRORLOG` | Startup, failed logins, errors. Also readable with `EXEC sp_readerrorlog;`. |
| Windows | Event Viewer → Windows Logs → System | Service crashes and restarts (from the Service Control Manager). |
| ETL (Phase 4) | `F:\Logs\jobs\etl.log` | The last ETL run's full output (overwritten each run). Every run's summary is in `SalesDB.ops.etl_runs`. |
| SQL Agent jobs | `msdb` job history (section 13) | Each job's outcome and each step's message. Kept 30 days. |
| Maintenance commands | `DBA.dbo.CommandLog` | Every backup, index operation, and CHECKDB: start, end, errors. Kept 30 days. |
| Extended Events | `F:\XE\ShopLite_Diagnostics_*.xel` | Deadlocks, blocking over 10 s, requests over 2 s, errors of severity 16+ (section 17). Rolling: 10 files × 50 MB. |

Every API response carries an `X-Request-ID` header; the same ID is in the API log line, which links
a shopper's failed request to its log entry.

Watching live: `Get-Content F:\Logs\shoplite-api\shoplite-api.err.log -Tail 50 -Wait`

## 7. When the site is down: checklist

Work from the outside in, and stop at the first thing that's wrong.

1. **Is the VM running?** Run `az vm get-instance-view …` (section 4). If it's deallocated (the
   evening schedule, usually), `az vm start` and wait 2-3 minutes.
2. **What does `/health` say from the Mac?** Use the table in section 5 to see which layer fails.
3. **RDP in and check the services:** `Get-Service MSSQLSERVER, shoplite-api, caddy`.
   - A service is **stopped:** start it (for the API, `C:\atlas\shoplite\api\deploy\restart-api.ps1`;
     otherwise `Start-Service <name>`), then read its log (section 6) to find out why.
   - **`shoplite-api` keeps restarting:** the end of `shoplite-api.err.log` shows the error. Common
     causes: an invalid setting in `.env` (the API names it at startup), or SQL Server unavailable.
4. **SQL Server:** `sqlcmd -S localhost -E -Q "SELECT @@VERSION"`. If it fails, read `ERRORLOG`.
   Check free space on F: (data and log files) and C:.
5. **Caddy and the certificate:** search `caddy.log` for "error" or "certificate". Let's Encrypt needs
   port 80 reachable from the internet, so check the NSG and the Windows Firewall rules.
6. **The storefront only:** if `/health` is fine but the site fails in the browser, open the browser's
   developer tools. A **CORS error** is covered in section 9. Calls going to the wrong host mean
   `NEXT_PUBLIC_API_URL` is wrong in Vercel and needs a redeploy.
7. **After a fix:** check `/health` from the Mac again, and place a test order on the storefront.

## 8. Config change didn't take effect

**Symptom:** you edited `C:\atlas\shoplite\api\.env` (for example `ALLOWED_ORIGINS`) and restarted, but
the API still behaves as before.

**What happened in production:** the API used to run with `--workers 2`. On Windows, those worker
processes could survive the service stop. Once their parent was gone, nothing linked them to the
service, so they kept listening on port 8000 with the **old** settings, while the restarted service
logged the new ones. The API now runs as a single process, and `restart-api.ps1` removes leftovers.
These checks still tell you what's really answering.

1. **The startup log line:** what the newest process read from `.env`.

   ```powershell
   Select-String -Path F:\Logs\shoplite-api\shoplite-api.err.log -Pattern "starting; session tag" |
       Select-Object -Last 3
   ```

   It prints the version, session tag, allowed origins, and feature flags. Correct values here prove
   the new process read the change, **not** that it's the one answering requests.

2. **The process list, with creation times:**

   ```powershell
   Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
       Sort-Object CreationDate |
       Format-Table ProcessId, ParentProcessId, CreationDate, CommandLine -Wrap
   ```

   Any API Python process **started before** your restart is a leftover. Don't filter by
   `*shoplite\api*`: the venv launcher starts `C:\Program Files\Python312\python.exe`, and
   multiprocessing workers start with a generic command line, so they'd be missed.

3. **Who owns port 8000:**

   ```powershell
   Get-NetTCPConnection -LocalPort 8000 -State Listen |
       Format-Table LocalAddress, OwningProcess, CreationTime
   Get-Process -Id <OwningProcess> | Format-List Id, StartTime, Path
   ```

   After `Stop-Service shoplite-api`, this must return **nothing**. If something is still listening,
   that's the leftover answering your requests.

4. **Ask the running API directly** (bypassing Caddy), for example for CORS:

   ```powershell
   (Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing `
       -Headers @{ Origin = 'https://shoplite-aadarsh.vercel.app' }).Headers['Access-Control-Allow-Origin']
   ```

   It should print `https://shoplite-aadarsh.vercel.app`. Empty means the process answering doesn't allow that origin.

**Fix:** `C:\atlas\shoplite\api\deploy\restart-api.ps1`. It stops the service, kills every `python.exe`
running as LOCAL SERVICE plus anything else Python on port 8000 (listing each one with its start
time), confirms the port is free, starts the service, and waits for `/health`. Use it after every
`.env` change, never a bare `Restart-Service`.

## 9. CORS errors in the browser

The API only answers browsers on the storefront's production origin:
**`https://shoplite-aadarsh.vercel.app`** (the `ALLOWED_ORIGINS` value in the API's `.env`).

- **Read the origin in the error.** The browser's console message names the origin the page was loaded
  from, e.g. "...from origin 'https://...' has been blocked by CORS policy". Compare it, character
  for character, with `ALLOWED_ORIGINS`: https, no trailing slash, exact host.
- **Only the production domain is allowed.** Vercel's longer deployment domain redirects to
  `https://shoplite-aadarsh.vercel.app`, so shoppers always end up on the allowed origin. Preview and branch URLs (the
  ones with `-git-` in them) are **blocked on purpose**; test those against a local API instead.
- **See the API's CORS answer with curl** from the Mac (the `Origin` header is what a browser sends):

  ```bash
  # Allowed origin: expect "access-control-allow-origin: https://shoplite-aadarsh.vercel.app"
  curl -s -o /dev/null -D - -H "Origin: https://shoplite-aadarsh.vercel.app" \
    https://shoplite-aadarsh.northcentralus.cloudapp.azure.com/health | grep -i '^access-control'

  # A blocked origin (e.g. a -git- preview): expect NO access-control-allow-origin line
  curl -s -o /dev/null -D - -H "Origin: https://shoplite-aadarsh-git-main-example.vercel.app" \
    https://shoplite-aadarsh.northcentralus.cloudapp.azure.com/health | grep -i '^access-control'

  # The preflight a browser sends before POSTs with custom headers: expect 200 (blocked: 400)
  curl -s -o /dev/null -w "%{http_code}\n" -X OPTIONS \
    -H "Origin: https://shoplite-aadarsh.vercel.app" \
    -H "Access-Control-Request-Method: POST" \
    -H "Access-Control-Request-Headers: content-type,x-customer-id" \
    https://shoplite-aadarsh.northcentralus.cloudapp.azure.com/api/checkout
  ```

- **curl says allowed but the browser says blocked?** An old API process may be answering; see
  section 8.
- **Changing the allowed origin:** edit `ALLOWED_ORIGINS` (comma-separated for several), then run
  `restart-api.ps1`.

## 10. Routine tasks

**Deploy a new API version** (details in `shoplite/api/deploy/README.md`):
push to `main` from the Mac, then on the VM run
`C:\atlas\shoplite\api\deploy\deploy.ps1 -Version x.y.z`. It pulls, installs, sets the version,
restarts through `restart-api.ps1` (clearing any leftover process), and waits for `/health` to
report the new version. To roll back, check out the previous
commit and deploy it with `-SkipPull` and a new version number.

**RDP stops connecting (the owner's IP changed):** set the new IP in `rdpSourceAddressPrefix` in
`infra/prod.parameters.local.json` and redeploy the Bicep (`infra/README.md`).

**The Vercel domain changed, or a new one was added:** edit `ALLOWED_ORIGINS` in
`C:\atlas\shoplite\api\.env` (exact `https://…`, no trailing slash, comma-separated), then run
`restart-api.ps1`.

**The API's address changed** (new DNS label): update `NEXT_PUBLIC_API_URL` in Vercel and
**redeploy the storefront**, because the value is fixed at build time. Update `SHOPLITE_API_HOST` in
`caddy.xml`, push, then on the VM **`git pull` first** and copy it to `C:\services\caddy\caddy-svc.xml`
and reinstall the Caddy service. (An out-of-date clone once installed `caddy-svc.xml` with the
placeholder host.)

**Rotate the `shoplite_app` password:** change it in all three places, with the **identical** value:
`ALTER LOGIN shoplite_app WITH PASSWORD = '<new>';` (as the SQL admin), `DB_PASSWORD` in the API
`.env`, and `SHOPLITE_APP_PASSWORD` in the root `.env`; then run `restart-api.ps1`. Any mismatch shows
up as error 18456 and a 503 from `/health`.

**Rotate the admin token:** set a new `ADMIN_TOKEN` in the API `.env`, then run `restart-api.ps1`.

**Change the shutdown time:** set `shutdownTime` (UTC) in the parameters file and redeploy the Bicep.

**Rotate the backup SAS (before it expires; Phase 4):** create a new SAS on the `sqlbackups` container
(read, write, create, list; HTTPS only; 12 months), then on the VM run
`.\server\backups\01-create-backup-credential.ps1 -ContainerUrl <container URL>`. It replaces the
secret in the existing credential and test-writes a backup. Update the expiry in section 3. If the
SAS **leaked**, rotate the storage key that signed it first (`server/backups/README.md`).

**Rotate the `shoplite_etl` password:** run `server\etl\sql\002_etl_login.sql` with the new
`SHOPLITE_ETL_PASSWORD` (it resets the password), then set the identical value as `SALESDB_PASSWORD`
in `C:\atlas\server\etl\.env`. The next ETL run proves it (exit code 3 and error 18456 if they differ).

**Change a job's schedule:** edit `server/jobs/sql/040_jobs.sql` and `server/jobs/schedule.md`,
push, pull on the VM, and run `040` again (same command as in `server/jobs/README.md`). Jobs keep
their history.

**Upgrade Ola Hallengren's scripts:** download the new `MaintenanceSolution.sql`, make the same two
edits (`USE [DBA]`, `@CreateJobs = 'N'`), run it, and note the new version here.
Installed version: _(fill in after installing)_.

## 11. Rules for production

- **Never run the API smoke tests against production.** They create orders and buy up stock.
- **Never run `apply_schema.py` against production again.** Script 001 drops and recreates SalesDB.
- **Don't edit code on the VM.** Change it on the Mac, push, and deploy. `deploy.ps1` refuses to run
  over local changes.
- **Don't open port 1433 or 8000** in the NSG or Windows Firewall, for any reason.
- **Restart the API with `restart-api.ps1`**, never a bare `Restart-Service`, so leftovers can't keep
  serving old settings.
- **`git pull` on the VM before copying any service file** (`shoplite-api.xml`, `caddy.xml`) into
  `C:\services`.
- **Deallocate the VM** when you're done working on it, unless something needs it running.
- **Never switch SalesDB to SIMPLE recovery** (Phase 4). It breaks the log chain: no point-in-time
  restore until the next full backup, and the restore test fails.
- **Ad-hoc backups are always `COPY_ONLY`.** A normal full backup taken by hand becomes the base for
  the next differentials, and a log backup taken by hand to anywhere else takes part of the chain with
  it. `COPY_ONLY` touches neither.
- **Never run `server/etl/sql/900_cutover_placeholders.sql` again.** It was a one-time step and
  refuses after the first successful ETL run anyway.

## 12. Simulated traffic (Phase 3)

Production has customers on weekdays without anyone doing anything: simulated shoppers in real
browsers, background API load, and a nightly restock. All of it runs from GitHub Actions, never on
the VM. Details: `.github/workflows/README.md`.

| Source | Schedule (UTC, Mon–Fri) | Tag in the API log |
|---|---|---|
| Playwright shoppers (`shoppers.yml`) | every 20 min, 13:10–22:50; 2 browsers for 5 minutes | `synthetic playwright/<persona>` (browser, searcher, buyer, loyal, admin) |
| Locust load (`load.yml`) | 10 min at :35, 13:35–22:35, plus Fridays 16:00 and 17:00 for the flash sale | `synthetic locust/<type>` (browse, search, buyer, loyal, admin) |
| Restock (`restock.yml`) | 22:30 (Phase 4: moved from 22:45 to land before the 22:45 and 22:50 log backups) | `synthetic workflow/restock` |
| Real people (you, friends) | any time | `synthetic -` |

### Pausing traffic (the kill switch)

Set the repository variable **`TRAFFIC_ENABLED`** to `false` (Settings → Secrets and variables →
Actions → Variables). Shoppers, load, and restock then show as **skipped**. The VM schedule keeps
running; disable `vm-schedule.yml` too if the VM should stay off. **Pause traffic before
maintenance, deployments, and incident experiments**, and set it back to `true` afterwards.

Every traffic workflow also checks `/health` first and skips (green run, "skipped" in the summary)
when the VM is off or the API is unhealthy.

### Reading a run

- **Shopper run:** the run summary has an outcome table (`paid`, `declined`, `out_of_stock`,
  `abandoned`, `browsed`, `searched`, `empty_search`, `reordered`, `admin_done`, `unavailable`,
  `error`), a persona table, and the API status codes the browsers saw. Only `error` (a broken page)
  turns the run red; then the `shoppers-failure-<id>` artifact has a screenshot and a trace (open the
  trace at https://trace.playwright.dev). An error's detail names the action, the `data-testid`
  element, Playwright's reason, and the last step.
- **Locust run:** the `locust-report-<id>` artifact has per-endpoint request counts, failures, and
  response-time percentiles. A red run means Locust counted failures: 503s or unexpected errors.
- **Restock run:** the summary shows products restocked, units added, and how many are still low.

### What normal looks like

**Volume:** on a normal business day, roughly 100–150 new orders in total (shoppers about 50–70,
Locust about 35–80), a peak of a few requests per second at midday and early evening, and a short
burst during the Friday flash sale. Weekends and nights: nothing (the VM is off).

**API log** (`F:\Logs\shoplite-api\shoplite-api.err.log`): mostly `200`s tagged `synthetic playwright/…`
and `synthetic locust/…`; a few `409`s (stock ran out, normal); `201`s on `/api/checkout`; occasional
`401`s only from a misconfigured admin token. **503s are not normal**: see section 7.

**Outcomes:** about 2% of payments declined (the simulated provider), a few `out_of_stock`, many
`browsed` and `searched`, and some `empty_search` (typos and things the store doesn't sell, on
purpose).

**SQL Server** (as the SQL admin, `sqlcmd -S localhost -E -d SalesDB`):

```sql
-- Today's orders by hour (UTC) and how they ended
SELECT DATEPART(hour, created_at) AS hour_utc,
       COUNT(*) AS orders,
       SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid,
       SUM(CASE WHEN status = 'payment_failed' THEN 1 ELSE 0 END) AS declined
FROM dbo.orders
WHERE created_at >= CAST(SYSUTCDATETIME() AS date)
GROUP BY DATEPART(hour, created_at)
ORDER BY hour_utc;

-- Open carts (abandoned carts accumulate; that's realistic)
SELECT COUNT(*) AS open_carts FROM dbo.carts WHERE status = 'open';

-- Low stock before/after the nightly restock
SELECT COUNT(*) AS at_or_below_reorder FROM dbo.inventory WHERE quantity_on_hand <= reorder_level;
```

Synthetic orders are ordinary orders for randomly chosen demo customers; only the API log's tag tells
them apart. That's on purpose: the database and the API treat them exactly like real ones.

**CPU credits:** under normal traffic, **CPU Credits Remaining** (VM → Metrics) should stay well
above zero all day. If it trends toward zero, lower `LOAD_PEAK_USERS` or `SHOPPERS_CONCURRENCY`
(repository variables). Running out of credits on purpose is a Phase 5 incident, not normal traffic.

## 13. Database jobs (Phase 4)

SQL Server Agent runs every database job inside the VM's 13:00–23:00 UTC weekday window. A job
scheduled while the VM is off simply never runs, with no error anywhere, so none is. The full
timetable, together with the GitHub workflows, in UTC and Boston time: **`server/jobs/schedule.md`**.
Definitions: `server/jobs/sql/040_jobs.sql` (all jobs are in category **ShopLite**, owned by `sa`).

| Job | When (UTC) | What |
|---|---|---|
| Backup SalesDB LOG | every 15 min 13:15–22:45, and 22:50, weekdays | Transaction log backup |
| Vendor ETL | 14:00 weekdays | Vendor feed into SalesDB (section 15) |
| ReportingDB refresh | 14:30 weekdays | Incremental refresh (section 16) |
| Backup SalesDB DIFF | 17:00 weekdays | Differential backup |
| Restore test | 20:00 Wednesday | Full + diff + logs restored to `SalesDB_RestoreTest`, CHECKDB, dropped (section 14) |
| ReportingDB full refresh | 20:30 Friday | Full rebuild |
| Integrity check | 21:00 Thursday | DBCC CHECKDB on SalesDB, ReportingDB, DBA, system databases |
| Backup SalesDB FULL | 21:30 weekdays | Full backup |
| Backup system, ReportingDB, DBA FULL | 21:45 Friday | master, model, msdb, ReportingDB, DBA |
| Index and statistics maintenance | 22:00 weekdays | Ola IndexOptimize; starts nothing new after 30 minutes |
| History cleanup | 22:35 weekdays | Backup history, job history, CommandLog older than 30 days |

No email alerts (no Database Mail). Failures show in job history, `DBA.dbo.CommandLog`, and the run
tables (`ops.etl_runs`, `ops.refresh_runs`, `DBA.dbo.restore_test_runs`), which monitoring (Phase 5)
watches.

**Last run of every job** (as the SQL admin):

```sql
WITH last_run AS (
    SELECT h.job_id, h.run_status, msdb.dbo.agent_datetime(h.run_date, h.run_time) AS ran_at,
           h.run_duration, h.message,
           ROW_NUMBER() OVER (PARTITION BY h.job_id ORDER BY h.instance_id DESC) AS n
    FROM msdb.dbo.sysjobhistory AS h WHERE h.step_id = 0
)
SELECT j.name, r.ran_at,
       CASE r.run_status WHEN 1 THEN 'succeeded' WHEN 0 THEN 'FAILED' WHEN 3 THEN 'cancelled' ELSE 'other' END AS outcome,
       r.run_duration AS hhmmss, LEFT(r.message, 200) AS message
FROM msdb.dbo.sysjobs AS j
JOIN msdb.dbo.syscategories AS c ON c.category_id = j.category_id AND c.name = 'ShopLite'
LEFT JOIN last_run AS r ON r.job_id = j.job_id AND r.n = 1
ORDER BY j.name;
```

A failed job's **step** row has the full error:
`SELECT TOP 5 * FROM msdb.dbo.sysjobhistory WHERE step_id > 0 AND run_status = 0 ORDER BY instance_id DESC;`

**Run a job now:** `EXEC msdb.dbo.sp_start_job N'ShopLite - Backup SalesDB LOG';` (it only starts the
job; read the history after it finishes).

**No job ran at all today:** SQL Agent isn't running. `Get-Service SQLSERVERAGENT`; its startup type
must be Automatic (`server\jobs\01-prepare-server.ps1` sets it).

## 14. Backups and restores (Phase 4)

### What exists

| | |
|---|---|
| Where | Blob container `sqlbackups` in the storage account `stshoplite…`, written with `BACKUP ... TO URL` through the SAS credential (section 3). Folders by server, database, and type (Ola's layout). |
| SalesDB | FULL recovery. Full daily 21:30, differential 17:00, log every 15 minutes 13:15–22:45 plus 22:50 (weekdays). Compressed, checksummed, verified after writing. |
| Others | master, model, msdb, ReportingDB, DBA: full, weekly (Friday 21:45). ReportingDB can also be rebuilt from SalesDB at any time. |
| Retention | 14 days: the storage lifecycle rule deletes older files (about once a day). |
| **RPO** (how much data could be lost) | About **15 minutes** during business hours. Changes after 22:50 stay in the log on the data disk overnight (deallocation keeps disks) and go into the next weekday's 13:15 log backup; losing the disk in between would lose those minutes. |
| **RTO** (how long a restore takes) | Measured weekly: `restore_seconds` of the latest restore test (below). Recorded first run: _(fill in after Part C3, step 6)_ |
| Point in time | Any moment covered by the log chain in the last 14 days, i.e. during business hours. |

### Are the backups healthy?

```sql
-- Latest backup of each type for SalesDB (finish times are UTC: the VM runs in UTC)
SELECT type AS backup_type,        -- D full, I differential, L log
       MAX(backup_finish_date) AS latest,
       DATEDIFF(MINUTE, MAX(backup_finish_date), GETDATE()) AS minutes_ago
FROM msdb.dbo.backupset
WHERE database_name = 'SalesDB' AND is_copy_only = 0
GROUP BY type;

-- Latest restore tests
SELECT TOP 5 run_id, started_at, status, full_backup_finished_at, log_backups_restored, restored_to,
       restore_seconds, checkdb_seconds, LEFT(error_message, 200) AS error
FROM DBA.dbo.restore_test_runs ORDER BY run_id DESC;
```

Healthy in business hours: the log backup under 20 minutes old, the differential from today after
17:00, the full from last night, and a `succeeded` restore test in the last week. In the portal, the
container's folders should show new files every 15 minutes during the day.

| Symptom | Likely cause |
|---|---|
| Every backup fails, "Operating system error 86" or HTTP 403 | The SAS expired or was revoked: rotate it (section 10) |
| Log backup fails: "no current database backup" | The chain was broken (SIMPLE recovery, or a restore over SalesDB). The job takes a full instead (`@ChangeBackupType`), which starts a new chain |
| Restore test: "The log chain is broken" | A log backup is missing (look at the LOG job's history around that time) or someone switched to SIMPLE |
| Restore test: CHECKDB errors | Possible corruption: run the Integrity check job on SalesDB now, keep every backup, and don't restore over SalesDB until you know which copy is damaged |
| Storage grows past a few GB | The lifecycle rule is missing or disabled (portal → storage account → Lifecycle management) |

### Restore SalesDB to a point in time (manual)

For "someone deleted or broke data at 15:42 and we need it back". **Never restore over SalesDB
directly**: restore a copy next to it, check it, then copy the missing data back. That keeps
everything that happened since, and it's undoable.

**1. Stop the damage:** set `TRAFFIC_ENABLED=false`. Note the stop time in **UTC**, just before the
damage (e.g. `2026-12-03T15:41:00`).

**2. Find the files**, as the SQL admin on the VM:

```sql
DECLARE @stop DATETIME = '2026-12-03T15:41:00';         -- UTC
DECLARE @full_finished DATETIME = (
    SELECT MAX(backup_finish_date) FROM msdb.dbo.backupset
    WHERE database_name = 'SalesDB' AND type = 'D' AND is_copy_only = 0 AND backup_finish_date <= @stop);

SELECT b.type, b.backup_start_date, b.backup_finish_date, m.physical_device_name
FROM msdb.dbo.backupset AS b
JOIN msdb.dbo.backupmediafamily AS m ON m.media_set_id = b.media_set_id
WHERE b.database_name = 'SalesDB' AND b.is_copy_only = 0
  AND b.backup_finish_date >= @full_finished
  AND b.backup_start_date <= DATEADD(HOUR, 1, @stop)
ORDER BY b.backup_finish_date;
```

From that list you need: the **full** (first row); the **latest differential (I) that finished before
the stop time**, if any; then **every log (L) after that differential** (or after the full, if there's
no differential), **up to and including the first log that finished after the stop time**.

**3. Get the logical file names** from the full backup:

```sql
RESTORE FILELISTONLY FROM URL = N'<full backup URL>';     -- LogicalName column, e.g. SalesDB and SalesDB_log
```

**4. Restore the copy** as `SalesDB_PITR`. Every step `NORECOVERY`; `STOPAT` on every log (a log that
ends before the stop time is applied whole; the one that contains it stops there):

```sql
RESTORE DATABASE SalesDB_PITR FROM URL = N'<full URL>'
    WITH NORECOVERY, CHECKSUM,
         MOVE N'SalesDB'     TO N'F:\SQLData\SalesDB_PITR.mdf',
         MOVE N'SalesDB_log' TO N'F:\SQLLog\SalesDB_PITR_log.ldf';
RESTORE DATABASE SalesDB_PITR FROM URL = N'<differential URL>' WITH NORECOVERY;   -- skip if none
RESTORE LOG SalesDB_PITR FROM URL = N'<log 1 URL>' WITH NORECOVERY, STOPAT = '2026-12-03T15:41:00';
RESTORE LOG SalesDB_PITR FROM URL = N'<log 2 URL>' WITH NORECOVERY, STOPAT = '2026-12-03T15:41:00';
-- ... every log in the list, in order ...
RESTORE DATABASE SalesDB_PITR WITH RECOVERY;
```

**5. Check and copy back.** Compare the copy with SalesDB (e.g. the deleted rows exist in
`SalesDB_PITR` and not in `SalesDB`), then copy just those rows back into SalesDB with an
`INSERT ... SELECT` from `SalesDB_PITR` (identity columns need `SET IDENTITY_INSERT ... ON`). Check
the storefront, set `TRAFFIC_ENABLED=true`.

**6. Clean up:** `DROP DATABASE SalesDB_PITR;` (disk space on F: is limited).

Replacing SalesDB **entirely** with a restored copy is a bigger decision (it throws away everything
since the stop time): it needs a tail-log backup first (`BACKUP LOG SalesDB TO URL = ... WITH
NORECOVERY`), the API stopped, and a new full backup afterwards to start a fresh chain. Plan it; don't
improvise it.

The weekly restore test does steps 2–4 automatically (to the latest point, not a chosen time), which
is why its success means this procedure will work.

## 15. The vendor feed: ETL and data contract (Phase 4)

**What it is:** the vendor's supplier catalog, prices, and stock (Neon Postgres, schema `supply`) are
loaded into `SalesDB.vendor.supplier_prices` by `server/etl/run_etl.py`, weekdays at 14:00 UTC
(SQL Agent job "Vendor ETL"), after the vendor's own daily changes at 12:00. The Phase 1 placeholder
rows were removed in the cutover; everything in that table now comes from the vendor.

**How:** extract → validate → stage → merge. It reads the vendor as `shoplite_reader` in one
read-only snapshot, validates it against the data contract, bulk-loads it into
`vendor.supplier_prices_staging`, and merges it in one short transaction (insert new, update changed,
flag vanished items as discontinued with stock 0; never deletes). Running it twice changes nothing.

**The data contract:** `knowledge/contracts/vendor-supplier-feed.yml` (version 1.0.0): the tables,
columns, types, keys, value rules (SKU format, cost ≥ 0, USD), minimum row counts, every item having
a price and stock row, and **freshness: the newest price change within 36 hours**. Any difference
stops the load **before** SalesDB changes, with a message naming it. The vendor may add columns;
renaming, removing, or retyping one breaks the contract. Changing the vendor's schema on purpose
means changing the contract and the ETL in the same commit.

**Every run** is a row in `SalesDB.ops.etl_runs`:

```sql
SELECT TOP 10 run_id, started_at, DATEDIFF(SECOND, started_at, finished_at) AS seconds, status,
       rows_read, rows_inserted, rows_updated, rows_discontinued, rows_unchanged, rows_skipped,
       vendor_newest_price_at, error_stage, LEFT(error_message, 300) AS error
FROM SalesDB.ops.etl_runs ORDER BY run_id DESC;
```

**Normal:** `succeeded` in seconds; about 10,000 read; about 3,000 updated, 20 discontinued, 10
inserted; `rows_skipped` 0.

| You see | Likely cause and what to do |
|---|---|
| `failed`, `validate`, "hours old" | The vendor stopped updating: `vendor-day.yml` didn't run, was disabled (60-day rule), or `VENDOR_ACTIVITY_ENABLED=false`. Run vendor-day by hand, then the ETL job. |
| `failed`, `validate`, "column missing" / "type expected" | The vendor changed their schema. Nothing was loaded; yesterday's data stays. Decide: the vendor reverts, or the contract and ETL change together. |
| `failed`, `validate`, "fewer than the contract minimum" | A half-loaded or wrong vendor database. Don't loosen the minimum; find out what happened upstream. |
| `failed`, `connect` | Neon unreachable, or the URL/password in `C:\atlas\server\etl\.env` is wrong. Neon's free tier waking up takes seconds, within the 15-second timeout. |
| `failed`, `merge`, "Refusing to merge" | Staging had fewer rows than the minimum: the safety stop against discontinuing the whole catalog. |
| `running`, no `finished_at`, hours old | The process was killed or the VM was deallocated mid-run. The next run works normally. |
| No row today, job failed with exit code 2, 3, or 4 | Configuration (2), SalesDB login (3), or another run held the lock (4): `F:\Logs\jobs\etl.log` has the message. |

Exit codes 11–15 mean connect, validate, extract, stage, merge (and there is a `failed` row). Full
details: `server/etl/README.md`.

**Run it by hand:** `EXEC msdb.dbo.sp_start_job N'ShopLite - Vendor ETL';`, or on the VM
`C:\atlas\server\etl\.venv\Scripts\python C:\atlas\server\etl\run_etl.py` (add `--check` to only
validate the vendor against the contract).

## 16. ReportingDB (Phase 4)

Daily sales summaries (by day, product, ship state, category) rebuilt from SalesDB by
`ReportingDB.rpt.usp_refresh_sales`: incremental weekdays at 14:30 (the last 3 days plus any gap, such
as the weekend), full rebuild Fridays at 20:30. Same rules as the admin reports: paid, shipped, or
delivered; item revenue without shipping; UTC dates. The storefront's admin reports still query
SalesDB; ReportingDB is for Grafana and forecasting later.

```sql
SELECT TOP 5 run_id, mode, from_date, status, DATEDIFF(SECOND, started_at, finished_at) AS seconds,
       sale_lines, days_rebuilt, rows_written, LEFT(error_message, 200) AS error
FROM ReportingDB.ops.refresh_runs ORDER BY run_id DESC;
```

To check it against SalesDB, run `server/reporting/queries/verify.sql`: every Part 2 row should say
PASS. If the numbers look wrong, `EXEC ReportingDB.rpt.usp_refresh_sales @mode = 'full';` rebuilds
everything (a few minutes; do it with traffic off).

## 17. Extended Events (Phase 4)

The session **`ShopLite_Diagnostics`** starts with SQL Server and writes to **`F:\XE`**
(`ShopLite_Diagnostics_<number>.xel`, rolling: at most 10 files of 50 MB; the oldest is deleted).
Events reach the file within 30 seconds. It records:

| Event | When |
|---|---|
| `xml_deadlock_report` | Every deadlock, with the full graph (who held what, who waited) |
| `blocked_process_report` | A session blocked for 10+ seconds (`blocked process threshold`), with blocker and blocked statements |
| `rpc_completed`, `sql_batch_completed` | Requests taking over 2 seconds |
| `error_reported` | Errors of severity 16 or higher |

Each event records the application (`client_app_name`: `ShopLite-API/<version>`, `ShopLite-ETL`, ...),
database, login, and host.

```sql
-- What's in the files, per event type
SELECT object_name AS event, COUNT(*) AS events, MIN(timestamp_utc) AS first, MAX(timestamp_utc) AS last
FROM sys.fn_xe_file_target_read_file(N'F:\XE\ShopLite_Diagnostics*.xel', NULL, NULL, NULL)
GROUP BY object_name;

-- The latest deadlock graphs (open the XML in SSMS to see the graph)
SELECT TOP 5 timestamp_utc, CAST(event_data AS XML) AS deadlock
FROM sys.fn_xe_file_target_read_file(N'F:\XE\ShopLite_Diagnostics*.xel', NULL, NULL, NULL)
WHERE object_name = 'xml_deadlock_report'
ORDER BY timestamp_utc DESC;
```

Is it running? `SELECT name FROM sys.dm_xe_sessions WHERE name = 'ShopLite_Diagnostics';` returns a
row. If not, `ALTER EVENT SESSION ShopLite_Diagnostics ON SERVER STATE = START;`, and check that
`F:\XE` exists and `NT SERVICE\MSSQLSERVER` can write to it (`server\jobs\01-prepare-server.ps1`).
