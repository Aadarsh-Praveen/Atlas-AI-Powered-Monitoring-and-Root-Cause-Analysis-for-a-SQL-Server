# Runbook: ShopLite production

How ShopLite runs in production, how to operate it day to day, and what to check when something looks
wrong. Written in Phase 2; the first document in `knowledge/`. Atlas reads it from Phase 7 onward.

**Last verified against the real setup:** _(fill in the date after Phase 2, Part C6)_

---

## 1. What runs where

```
Shopper's browser ──HTTPS──▶ Vercel: ShopLite storefront (Next.js)
        │
        └── API calls, HTTPS ──▶ <dns-label>.<region>.cloudapp.azure.com  (static public IP)
                                    Azure VM prod-sql-01
                                    ├─ Caddy           ports 443 and 80     service "caddy"
                                    ├─ ShopLite API    127.0.0.1:8000       service "shoplite-api"
                                    └─ SQL Server 2022 Developer, localhost:1433, database SalesDB
```

The storefront never talks to SQL Server. Every click in the browser becomes a request to the API,
and the API queries SalesDB.

| Component | Where | Details |
|---|---|---|
| Storefront | Vercel | Built from GitHub, root directory `shoplite/web`. Calls the API at `NEXT_PUBLIC_API_URL` (fixed at build time). |
| VM | Azure, resource group `rg-shoplite-prod` | `prod-sql-01`, Standard_B2ms (2 vCPU, 8 GB RAM), Windows Server 2022, time zone **UTC**. Tags `project=atlas`, `env=prod`, `app=shoplite`. Defined in `infra/` (Bicep). |
| Caddy | VM, service `caddy` | HTTPS front door. Let's Encrypt certificate for the DNS name, renewed automatically. Redirects HTTP to HTTPS and forwards to `127.0.0.1:8000`. Config: `C:\atlas\shoplite\api\deploy\Caddyfile`. |
| ShopLite API | VM, service `shoplite-api` | FastAPI on Uvicorn, **2 worker processes**, listening on **127.0.0.1:8000 only**. Runs as `NT AUTHORITY\LocalService`. Code in `C:\atlas`; settings in `C:\atlas\shoplite\api\.env`. |
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
| `shoplite_app` | The API | Least privilege in SalesDB: read/write data and EXECUTE only. Password = `SHOPLITE_APP_PASSWORD` (root `.env`) = `DB_PASSWORD` (API `.env`). |
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
curl -s https://<dns-label>.<region>.cloudapp.azure.com/health
```

| Response | Meaning |
|---|---|
| `200`, `{"status":"ok","version":"x.y.z","database":"ok"}` | API up and SalesDB reachable. `version` is the deployed release. |
| `503`, `"status":"unavailable","database":"unreachable"` | The API runs but can't reach SQL Server (SQL Server down, wrong password, or connection pool exhausted). |
| `502` from Caddy | Caddy runs but the API doesn't answer on 127.0.0.1:8000 (service stopped, crashing, or starting). |
| Connection timeout or refused | The VM is off or deallocated, Caddy is stopped, or ports 443/80 are blocked. |
| Certificate error | Caddy couldn't get or renew the certificate (see section 7). |

On the VM (PowerShell as Administrator):

```powershell
Get-Service MSSQLSERVER, shoplite-api, caddy                       # all three: Running
Invoke-RestMethod http://127.0.0.1:8000/health                       # bypasses Caddy
Get-Volume -DriveLetter C, F | Format-Table DriveLetter, SizeRemaining, Size
```

In SQL Server, the API's connections are tagged with the release:

```sql
SELECT program_name, COUNT(*) AS sessions
FROM sys.dm_exec_sessions
WHERE program_name LIKE N'ShopLite-API/%'
GROUP BY program_name;
```

Expect **up to 30** API sessions: 2 workers × (pool 10 + overflow 5). More than one `program_name` at
once means two releases are running, for example in the middle of a restart.

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
   - A service is **stopped:** `Start-Service <name>`, then read its log (section 6) to find out why.
   - **`shoplite-api` keeps restarting:** the end of `shoplite-api.err.log` shows the error. Common
     causes: an invalid setting in `.env` (the API names it at startup), or SQL Server unavailable.
4. **SQL Server:** `sqlcmd -S localhost -E -Q "SELECT @@VERSION"`. If it fails, read `ERRORLOG`.
   Check free space on F: (data and log files) and C:.
5. **Caddy and the certificate:** search `caddy.log` for "error" or "certificate". Let's Encrypt needs
   port 80 reachable from the internet, so check the NSG and the Windows Firewall rules.
6. **The storefront only:** if `/health` is fine but the site fails in the browser, open the browser's
   developer tools. A **CORS error** means the Vercel domain isn't exactly in `ALLOWED_ORIGINS`
   (section 8). Calls going to the wrong host mean `NEXT_PUBLIC_API_URL` is wrong in Vercel and needs
   a redeploy.
7. **After a fix:** check `/health` from the Mac again, and place a test order on the storefront.

## 8. Routine tasks

**Deploy a new API version** (details in `shoplite/api/deploy/README.md`):
push to `main` from the Mac, then on the VM run
`C:\atlas\shoplite\api\deploy\deploy.ps1 -Version x.y.z`. It pulls, installs, sets the version,
restarts, and waits for `/health` to report the new version. To roll back, check out the previous
commit and deploy it with `-SkipPull` and a new version number.

**RDP stops connecting (the owner's IP changed):** set the new IP in `rdpSourceAddressPrefix` in
`infra/prod.parameters.local.json` and redeploy the Bicep (`infra/README.md`).

**The Vercel domain changed, or a new one was added:** edit `ALLOWED_ORIGINS` in
`C:\atlas\shoplite\api\.env` (exact `https://…`, no trailing slash, comma-separated), then
`Restart-Service shoplite-api`.

**The API's address changed** (new DNS label): update `NEXT_PUBLIC_API_URL` in Vercel and
**redeploy the storefront**, because the value is fixed at build time. Update `SHOPLITE_API_HOST` in
`C:\services\caddy\caddy-svc.xml` and reinstall the Caddy service.

**Rotate the `shoplite_app` password:**
`ALTER LOGIN shoplite_app WITH PASSWORD = '<new>';` (as the SQL admin), update `DB_PASSWORD` in the
API `.env` and `SHOPLITE_APP_PASSWORD` in the root `.env`, then `Restart-Service shoplite-api`.

**Rotate the admin token:** set a new `ADMIN_TOKEN` in the API `.env`, then
`Restart-Service shoplite-api`.

**Change the shutdown time:** set `shutdownTime` (UTC) in the parameters file and redeploy the Bicep.

## 9. Rules for production

- **Never run the API smoke tests against production.** They create orders and buy up stock.
- **Never run `apply_schema.py` against production again.** Script 001 drops and recreates SalesDB.
- **Don't edit code on the VM.** Change it on the Mac, push, and deploy. `deploy.ps1` refuses to run
  over local changes.
- **Don't open port 1433 or 8000** in the NSG or Windows Firewall, for any reason.
- **Deallocate the VM** when you're done working on it, unless something needs it running.
