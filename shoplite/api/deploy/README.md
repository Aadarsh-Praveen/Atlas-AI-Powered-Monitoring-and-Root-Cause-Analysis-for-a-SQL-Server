# shoplite/api/deploy

Running the ShopLite API in production on `prod-sql-01`: two Windows services behind HTTPS.

```
Internet ──443/80──▶ Caddy (service "caddy")
                       │  Let's Encrypt certificate, HTTP→HTTPS redirect
                       ▼
                     127.0.0.1:8000 ── ShopLite API (service "shoplite-api", Uvicorn × 2 workers)
                                         │
                                         ▼
                                       SQL Server (localhost:1433, SalesDB, shoplite_app)
```

> **Never run the API smoke tests (`shoplite/api/tests`) against production.** They create orders and
> buy up scarce stock. Tests stay on your Mac.

## Files

| File | What it is |
|---|---|
| `shoplite-api.xml` | WinSW service definition for the API: Uvicorn, 2 workers, 127.0.0.1:8000, LocalService account, restart on crash, rotating logs |
| `Caddyfile` | Caddy configuration: HTTPS for your DNS name, redirect, reverse proxy to the API, JSON logs |
| `caddy.xml` | WinSW service definition for Caddy |
| `deploy.ps1` | Every later release: pull, install, set the version, restart, check `/health` |
| `.env.production.example` | Template for the API's production `.env` |

## Where things live on the VM

| Path | Contents |
|---|---|
| `C:\atlas` | The repo (cloned in `server/vm-setup/README.md`, step 5) |
| `C:\atlas\shoplite\api\.venv` | The API's own virtual environment |
| `C:\atlas\shoplite\api\.env` | The API's production settings (secrets; Administrators, SYSTEM, and LocalService only) |
| `C:\services\shoplite-api\` | `shoplite-api.exe` (WinSW) and a copy of `shoplite-api.xml` |
| `C:\services\caddy\` | `caddy.exe`, `caddy-svc.exe` (WinSW), `caddy-svc.xml`, and Caddy's `data` and `config` folders (certificates) |
| `F:\Logs\shoplite-api\` | `shoplite-api.out.log` and `.err.log` (rotated at 10 MB, 10 kept) |
| `F:\Logs\caddy\` | `access.log` (one JSON line per request), `caddy.log`, and WinSW's console logs |

---

## First-time install

Prerequisite: `server/vm-setup/README.md` is complete (data disk, SQL settings, firewall, Python for
all users, Git, ODBC 18, the repo at `C:\atlas`, SalesDB seeded and verified).

Run everything below in **PowerShell as Administrator** on the VM.

### 1. The API's production `.env`

```powershell
Copy-Item C:\atlas\shoplite\api\deploy\.env.production.example C:\atlas\shoplite\api\.env
notepad C:\atlas\shoplite\api\.env
```

- `DB_PASSWORD`: the `SHOPLITE_APP_PASSWORD` from the VM's root `.env`.
- `ADMIN_TOKEN`: a new token, generated in step 3 below (come back and fill it in).
- `ALLOWED_ORIGINS`: leave the placeholder until Vercel exists (Part C5).

Restrict the file to Administrators, SYSTEM, and the service account (read-only):

```powershell
icacls C:\atlas\shoplite\api\.env /inheritance:r /grant:r "Administrators:F" "SYSTEM:F" "NT AUTHORITY\LocalService:R"
```

### 2. Access for the service account

Both services run as `NT AUTHORITY\LocalService`. Give it read access to the code and write access
to the log folders:

```powershell
icacls C:\atlas /grant "NT AUTHORITY\LocalService:(OI)(CI)RX"

New-Item -ItemType Directory -Force F:\Logs\shoplite-api, F:\Logs\caddy | Out-Null
icacls F:\Logs\shoplite-api /grant "NT AUTHORITY\LocalService:(OI)(CI)M"
icacls F:\Logs\caddy        /grant "NT AUTHORITY\LocalService:(OI)(CI)M"
```

(`RX` = read and run; `M` = modify, which includes creating and rotating files.)

### 3. The API's virtual environment, then a test run

```powershell
cd C:\atlas\shoplite\api
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Generate the admin token, put it in .env (ADMIN_TOKEN), and save it in your password manager:
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"

# Test in the foreground (Ctrl+C to stop):
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second PowerShell window: `Invoke-RestMethod http://127.0.0.1:8000/health` should show
`status ok` and `database ok`. If the API refuses to start, its message names the setting that's
wrong. Stop the test with Ctrl+C before continuing.

### 4. Install the API service (WinSW)

Download **WinSW-x64.exe** from the v2.12.0 release:
https://github.com/winsw/winsw/releases/tag/v2.12.0

```powershell
New-Item -ItemType Directory -Force C:\services\shoplite-api | Out-Null
Copy-Item $HOME\Downloads\WinSW-x64.exe C:\services\shoplite-api\shoplite-api.exe
Copy-Item C:\atlas\shoplite\api\deploy\shoplite-api.xml C:\services\shoplite-api\shoplite-api.xml

C:\services\shoplite-api\shoplite-api.exe install
Start-Service shoplite-api
Get-Service shoplite-api                         # Status: Running
Invoke-RestMethod http://127.0.0.1:8000/health   # status ok
```

If it doesn't start, look in `F:\Logs\shoplite-api\shoplite-api.err.log`.

### 5. Install Caddy

Download Caddy for **Windows amd64** from https://caddyserver.com/download (standard features are
enough) and save it as `C:\services\caddy\caddy.exe`.

```powershell
New-Item -ItemType Directory -Force C:\services\caddy\data, C:\services\caddy\config | Out-Null
icacls C:\services\caddy\data   /grant "NT AUTHORITY\LocalService:(OI)(CI)M"
icacls C:\services\caddy\config /grant "NT AUTHORITY\LocalService:(OI)(CI)M"

Copy-Item $HOME\Downloads\WinSW-x64.exe C:\services\caddy\caddy-svc.exe
Copy-Item C:\atlas\shoplite\api\deploy\caddy.xml C:\services\caddy\caddy-svc.xml
notepad C:\services\caddy\caddy-svc.xml
```

The copy already names production's host, `shoplite-aadarsh.northcentralus.cloudapp.azure.com`
(`SHOPLITE_API_HOST`); check it matches the Bicep `fqdn` output. Then validate the configuration and
install:

```powershell
$env:SHOPLITE_API_HOST = "shoplite-aadarsh.northcentralus.cloudapp.azure.com"
C:\services\caddy\caddy.exe validate --config C:\atlas\shoplite\api\deploy\Caddyfile --adapter caddyfile

C:\services\caddy\caddy-svc.exe install
Start-Service caddy
Get-Service caddy
```

Caddy requests the certificate within a minute. Watch `F:\Logs\caddy\caddy.log` for "certificate
obtained successfully". If it fails, check that the NSG and Windows Firewall allow port 80, and that
`SHOPLITE_API_HOST` exactly matches the DNS name.

### 6. Check from your Mac

```bash
curl -i https://shoplite-aadarsh.northcentralus.cloudapp.azure.com/health   # 200, "status":"ok", valid certificate
curl -I http://shoplite-aadarsh.northcentralus.cloudapp.azure.com/health    # 308 redirect to https
nc -vz -w 5 shoplite-aadarsh.northcentralus.cloudapp.azure.com 1433         # must FAIL (time out or be refused)
```

Also try `https://shoplite-aadarsh.northcentralus.cloudapp.azure.com:8000/health`; it must fail, because the API only listens on
127.0.0.1.

### 7. Survive a restart

Restart the VM (`az vm restart -g rg-shoplite-prod -n prod-sql-01`), wait a couple of minutes, and
repeat the `/health` check from your Mac. Both services start on their own (automatic, delayed
start).

### 8. After Vercel exists (Part C5)

Put your Vercel production domain in `ALLOWED_ORIGINS` in `C:\atlas\shoplite\api\.env`, then:

```powershell
Restart-Service shoplite-api
```

---

## Later deploys

1. On your Mac: commit and push to `main`.
2. On the VM, in PowerShell as Administrator, with the next version number:

```powershell
C:\atlas\shoplite\api\deploy\deploy.ps1 -Version 1.0.1
```

It pulls (and refuses if the VM's copy has local changes), installs requirements, sets `APP_VERSION`,
restarts the API (and Caddy if the Caddyfile changed), and waits until `/health` reports the new
version. A failure prints the end of the error log and exits with code 1.

Version numbers: bump the last number for fixes (1.0.1), the middle one for features (1.1.0). The
version shows in `/health`, the `X-App-Version` header, and SQL Server's sessions
(`program_name = ShopLite-API/1.0.1`).

### Rolling back

Deploy the previous commit with a new version number, so the rollback is visible as its own release:

```powershell
git -C C:\atlas log --oneline -5                 # find the last good commit
git -C C:\atlas checkout <good-commit>
C:\atlas\shoplite\api\deploy\deploy.ps1 -Version 1.0.2 -SkipPull
```

Afterwards, fix the problem on your Mac, push, and return the VM to `main`
(`git -C C:\atlas checkout main`) before the next normal deploy.

### Changing a service definition

`deploy.ps1` doesn't reapply `shoplite-api.xml` or `caddy.xml` (it warns when one changed). To apply
one:

```powershell
Stop-Service shoplite-api
C:\services\shoplite-api\shoplite-api.exe uninstall
Copy-Item C:\atlas\shoplite\api\deploy\shoplite-api.xml C:\services\shoplite-api\shoplite-api.xml
C:\services\shoplite-api\shoplite-api.exe install
Start-Service shoplite-api
```

For Caddy it's the same with `caddy-svc.exe` and `caddy-svc.xml`; set `SHOPLITE_API_HOST` again in
the new copy (it already names production's host).

## Logs

```powershell
Get-Content F:\Logs\shoplite-api\shoplite-api.err.log -Tail 50 -Wait   # live API log (Ctrl+C to stop)
Get-Content F:\Logs\caddy\access.log -Tail 20                          # recent requests
```

Uvicorn writes its request and error lines to the `.err.log` (that's its standard stream for logs);
the `.out.log` is usually empty.
