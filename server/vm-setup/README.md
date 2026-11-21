# server/vm-setup

PowerShell scripts that prepare `prod-sql-01` after Bicep creates it (`infra/`). Run them **on the
VM**, over Remote Desktop, in order. Each one checks before it changes anything, so running a script
again is safe: it skips what's already done.

This guide is Phase 2, Part C3. It ends with SalesDB built and seeded with 1,000,000 orders. Part C4
(API and Caddy services) continues in `shoplite/api/deploy/README.md`.

## The scripts

| Order | Script | What it does | Changes the VM? |
|---|---|---|---|
| 1 | `01-prepare-data-disk.ps1` | Formats the raw data disk as **F:** (NTFS, 64 KB clusters), creates `F:\SQLData`, `F:\SQLLog`, `F:\Logs`, and gives SQL Server full control of its two folders | Yes |
| 2 | `02-configure-sql.ps1` | Max server memory 5 GB; default data/log paths on F: (restarts SQL Server only if they change); checks SQL authentication and your SQL admin login | Yes |
| 3 | `03-check-prerequisites.ps1` | Checks Python 3.12, Git, ODBC Driver 18, and UTC; prints download links for anything missing | No (read-only) |
| 4 | `04-configure-firewall.ps1` | Windows Firewall on; allow 80 and 443; block 1433 from outside the VM | Yes |

## Step by step

### 1. Connect to the VM

On your Mac, open **Windows App** (Microsoft's Remote Desktop app) and add a PC:

- **PC name:** `shoplite-aadarsh.northcentralus.cloudapp.azure.com` (the `fqdn` output from the Bicep deployment)
- **User account:** the Windows `adminUsername` and password from the deployment

If the connection times out, your public IP has probably changed since you deployed. Update
`rdpSourceAddressPrefix` and redeploy (`infra/README.md`, "Changing something later").

Check the time in the corner of the VM's screen: it should be UTC.

### 2. Get the scripts onto the VM

The repo isn't cloned yet (Git comes in step 3). For now, copy the `server\vm-setup` folder to
`C:\setup` on the VM. Windows App lets you paste files copied on the Mac straight into File Explorer
on the VM.

Then open **PowerShell as Administrator** (right-click the Start button, then **Terminal (Admin)**)
and allow local scripts for this window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
cd C:\setup\vm-setup
```

### 3. Run scripts 1 to 4

```powershell
.\01-prepare-data-disk.ps1
.\02-configure-sql.ps1 -SqlAdminUser <your sqlAdminUsername>
.\03-check-prerequisites.ps1
```

`03` will report what's missing on a fresh VM. Install these on the VM, each with its default options:

- **Python 3.12 (64-bit)** from https://www.python.org/downloads/windows/. Not the defaults here:
  on the first screen tick **Add python.exe to PATH** and choose **Customize installation**; on
  "Advanced Options" tick **Install Python 3.12 for all users**. It then installs under
  `C:\Program Files\Python312`. A default (per-user) install lives inside your admin profile, which
  the API service's account (LocalService) can't read, so the service would fail to start.
- **Git for Windows** from https://git-scm.com/download/win
- **ODBC Driver 18 for SQL Server (x64)** from
  https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server

Close the PowerShell window, open a **new** one as Administrator (so the new PATH is picked up), and
run `03` again until every line is OK. Then:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
cd C:\setup\vm-setup
.\04-configure-firewall.ps1
```

### 4. Check the result

```powershell
Get-Volume -DriveLetter F                                # SQLDATA, about 64 GB
Get-NetFirewallRule -Group ShopLite | Format-Table DisplayName, Enabled, Action
sqlcmd -S localhost -E -Q "SELECT SERVERPROPERTY('InstanceDefaultDataPath'), SERVERPROPERTY('InstanceDefaultLogPath')"
```

### 5. Clone the repo

Clone to a short path. Long Windows paths cause trouble with Python packages.

```powershell
git clone https://github.com/<you>/<repo>.git C:\atlas
cd C:\atlas
```

From now on, run the setup scripts from `C:\atlas\server\vm-setup`. You can delete `C:\setup`.

### 6. Create the root `.env` on the VM

This is a **new** file with **new** passwords, never a copy of your Mac's `.env`. Only the database
tools read it (`apply_schema.py`, `seed.py`); the API gets its own `.env` in Part C4.

```powershell
notepad C:\atlas\.env
```

```ini
# prod-sql-01 root .env: database tools only. Never commit.
SQL_ADMIN_USER=<your sqlAdminUsername>
SA_PASSWORD=<the SQL admin password you typed at the Bicep deploy>
SHOPLITE_APP_PASSWORD=<a NEW password for the shoplite_app login>
```

- `SQL_ADMIN_USER` and `SA_PASSWORD` are the SQL admin login and password created by the SQL IaaS
  extension. `SA_PASSWORD` keeps its name from local development, where the admin is `sa`.
- `SHOPLITE_APP_PASSWORD` is new. `apply_schema.py` creates the `shoplite_app` login with it, and the
  API's production `.env` will use the same value as `DB_PASSWORD`.
- Save both passwords in your password manager, and avoid the characters listed in `.env.example`.
- The Postgres lines from the Mac's `.env` aren't needed here.

Then restrict who can read the file to Administrators and SYSTEM:

```powershell
icacls C:\atlas\.env /inheritance:r /grant:r "Administrators:F" "SYSTEM:F"
```

### 7. Build and seed SalesDB

```powershell
cd C:\atlas
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r shoplite\db\requirements.txt

python shoplite\db\apply_schema.py
Measure-Command { python shoplite\db\seed.py --orders 1000000 | Out-Default }
```

- `apply_schema.py` connects as your SQL admin (`SQL_ADMIN_USER`) and creates SalesDB in
  `F:\SQLData` and `F:\SQLLog`, thanks to `02`'s default paths.
- `Measure-Command` times the seed run. Note the time; Phase 2 asks for it.
- **Never run `apply_schema.py` again on production once real orders exist:** script `001` drops and
  recreates SalesDB.

### 8. Verify

Run Part 2 of `verify.sql`. Every check should say PASS.

```powershell
sqlcmd -S localhost -E -I -d SalesDB -i shoplite\db\queries\verify.sql
```

`-I` turns on QUOTED_IDENTIFIER, which the filtered index needs (see `003_indexes.sql`). You can also
open the file in SSMS on the VM, which sets it by default.

Then confirm the files are on the data disk:

```powershell
sqlcmd -S localhost -E -Q "SELECT name, physical_name FROM sys.master_files WHERE database_id = DB_ID('SalesDB')"
```

Both paths should start with `F:\`.

**Done with C3.** Continue with Part C4 in `shoplite/api/deploy/README.md`, or deallocate the VM if
you're stopping for the day:

```bash
az vm deallocate -g rg-shoplite-prod -n prod-sql-01
```
