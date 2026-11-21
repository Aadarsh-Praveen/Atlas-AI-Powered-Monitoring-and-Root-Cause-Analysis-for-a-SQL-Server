# Runbook: ShopLite production

How ShopLite runs in production, how to operate it day to day, and what to check when something looks
wrong. Written in Phase 2; the first document in `knowledge/`. Atlas reads it from Phase 7 onward.

**Last verified against the real setup:** _(fill in the date after Phase 2, Part C6)_

---

## 1. What runs where

```
Shopper's browser ──HTTPS──▶ Vercel: ShopLite storefront, https://shoplite-aadarsh.vercel.app
        │
        └── API calls, HTTPS ──▶ shoplite-aadarsh.northcentralus.cloudapp.azure.com  (static public IP)
                                    Azure VM prod-sql-01
                                    ├─ Caddy           ports 443 and 80     service "caddy"
                                    ├─ ShopLite API    127.0.0.1:8000       service "shoplite-api"
                                    └─ SQL Server 2022 Developer, localhost:1433, database SalesDB
```

The storefront never talks to SQL Server. Every click in the browser becomes a request to the API,
and the API queries SalesDB.

| Component | Where | Details |
|---|---|---|
| Storefront | Vercel | **https://shoplite-aadarsh.vercel.app** (the production domain; Vercel's longer deployment domain redirects to it). Built from GitHub, root directory `shoplite/web`. Calls the API at `NEXT_PUBLIC_API_URL` = `https://shoplite-aadarsh.northcentralus.cloudapp.azure.com`, a Vercel **Config** variable: public on purpose, since it's copied into the JavaScript every browser downloads, and fixed at build time (changing it needs a redeploy). |
| VM | Azure, resource group `rg-shoplite-prod` | `prod-sql-01`, Standard_B2as_v2 (2 vCPU AMD, 8 GB RAM, burstable, no temporary disk), Windows Server 2022, time zone **UTC**. Region North Central US (`northcentralus`). Tags `project=atlas`, `env=prod`, `app=shoplite`. Defined in `infra/` (Bicep). |
| Caddy | VM, service `caddy` | HTTPS front door. Let's Encrypt certificate for the DNS name, renewed automatically. Redirects HTTP to HTTPS and forwards to `127.0.0.1:8000`. Config: `C:\atlas\shoplite\api\deploy\Caddyfile`. |
| ShopLite API | VM, service `shoplite-api` | FastAPI on Uvicorn, **one process** (no extra workers; see section 8), listening on **127.0.0.1:8000 only**. Runs as `NT AUTHORITY\LocalService`. Code in `C:\atlas`; settings in `C:\atlas\shoplite\api\.env`. Restart it with `C:\atlas\shoplite\api\deploy\restart-api.ps1`. |
| SQL Server | VM, service `MSSQLSERVER` | SQL Server 2022 Developer. Database `SalesDB` (about 1,000,000 orders). Accepts local connections only. Max server memory 5 GB. Recovery model SIMPLE (Phase 4 changes it to FULL). |

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

Both `.env` files on the VM are readable only by Administrators and SYSTEM (and LocalService, read-only,
for the API's).

## 4. Starting and stopping

The VM is **deallocated every evening** by Azure's auto-shutdown schedule (`shutdown-computevm-prod-sql-01`,
at the time set in Bicep, in UTC). Starting it is manual until Phase 3.

From the Mac:

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
Get-Service MSSQLSERVER, shoplite-api, caddy                       # all three: Running
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

## 6. Logs

All logs are on the data disk and rotate, so they can't fill it.

| Log | Path | Contents |
|---|---|---|
| API | `F:\Logs\shoplite-api\shoplite-api.err.log` | One line per request (method, path, status, duration, request ID) and errors with tracebacks. Rotated at 10 MB, 10 kept. (`.out.log` is usually empty.) |
| Caddy access | `F:\Logs\caddy\access.log` | One JSON line per request reaching Caddy, with client IP and status. Rotated at 10 MB, 10 kept. |
| Caddy | `F:\Logs\caddy\caddy.log` | Startup, configuration, and certificate events. |
| SQL Server | `C:\Program Files\Microsoft SQL Server\MSSQL16.MSSQLSERVER\MSSQL\Log\ERRORLOG` | Startup, failed logins, errors. Also readable with `EXEC sp_readerrorlog;`. |
| Windows | Event Viewer → Windows Logs → System | Service crashes and restarts (from the Service Control Manager). |

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
