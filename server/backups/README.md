# server/backups

Real backups for SalesDB. In production they go to **Azure Blob Storage** (`BACKUP ... TO URL`, the
private `sqlbackups` container from `infra/modules/storage.bicep`), off the VM, so they survive losing
the VM or its disks. Locally they go to a folder inside the SQL Server container.

This folder sets things up **once**: the credential that lets SQL Server write to Blob Storage, FULL
recovery, and the first full backup that starts the log chain. The **scheduled** backups (full,
differential, log every 15 minutes) and the weekly restore test are SQL Agent jobs in `server/jobs/`
(B5).

| File | What it does | Where |
|---|---|---|
| `01-create-backup-credential.ps1` | Asks for the container URL and a SAS token (hidden), checks the SAS, creates the SQL credential, tests it with a small backup and read-back | prod-sql-01 only |
| `02_full_recovery_and_first_backup.sql` | SalesDB → FULL recovery, first full backup (compressed, checksums), verifies it, shows the log chain started | both (`BACKUP_TARGET` = `URL` or `DISK`) |

## Key ideas

- **The SAS token** is a signed, expiring permission slip for one container: read, write, create, list,
  and nothing else. No storage account key ever goes on the VM. If the SAS leaks, the damage is limited
  to that container, it can't delete anything, and it expires.
- **The SQL credential** stores the SAS inside SQL Server (encrypted by the server). Its name is
  exactly the container URL: that's how `BACKUP ... TO URL = '<container URL>/file.bak'` finds it.
- **FULL recovery + log backups** make point-in-time restore possible. The first full backup is what
  starts the log chain; until it exists, SalesDB behaves as if it were still in SIMPLE recovery.
- **Recovery point (RPO):** about 15 minutes during business hours, once B5's log backups run. Changes
  made after the day's last log backup (22:50 UTC) and before the VM stops at 23:00 stay safe in the
  log on the data disk (deallocation keeps disks), and the next weekday's first log backup copies them
  out. They're in no backup until then, so losing the disk in between would lose those few minutes.

---

## Production (Part C3, in the Phase 4 maintenance window)

Order: deploy the storage (`infra/README.md`, "Phase 4: adding the backup storage") → **create the SAS**
→ **create the credential** → **FULL recovery and first backup**. `TRAFFIC_ENABLED=false` throughout.

### 1. Create the SAS

**Permissions: Read, Write, Create, List. Not Delete.** (Read is needed to restore from Blob Storage,
which the weekly restore test does; Delete isn't needed because the lifecycle rule deletes old
backups.) **HTTPS only. Expiry: 12 months from today.**

**Portal:** Storage account (`stshoplite…`) → **Data storage → Containers** → `sqlbackups` → **⋯ →
Generate SAS** →

| Setting | Value |
|---|---|
| Signing method | Account key |
| Signing key | Key 1 (note which: rotating that key later revokes the SAS) |
| Permissions | Read, Write, Create, List |
| Start | now |
| Expiry | 12 months from today |
| Allowed protocols | HTTPS only |

→ **Generate SAS token and URL** → copy the **Blob SAS token** (the part starting `sp=` or `sv=`), not
the URL. Keep it in your clipboard or password manager only; don't save it in a file.

**Or with the CLI** (from your Mac, signed in to Azure; prints the token):

```bash
az storage container generate-sas \
  --account-name <stshoplite...> --name sqlbackups \
  --permissions rwcl --https-only \
  --expiry $(date -u -v+12m +%Y-%m-%dT%H:%MZ) \
  --auth-mode key --output tsv
```

(`date -v+12m` is macOS syntax.)

### 2. Create the credential (on the VM)

PowerShell as Administrator, from `C:\atlas`:

```powershell
.\server\backups\01-create-backup-credential.ps1 -ContainerUrl https://<stshoplite...>.blob.core.windows.net/sqlbackups
```

Paste the SAS at the hidden prompt. Expect `[OK]` for the checks, `Credential CREATED`, a test backup
written and read back, and the expiry date. The script refuses (and changes nothing) if the SAS is
account-wide, allows HTTP, can delete, lacks a permission, or has expired.

**Record the expiry** in `knowledge/runbooks/shoplite-production.md` (backups section): "Backup SAS
expires YYYY-MM-DD; created with key 1." A forgotten expiry is a classic way for backups to start
failing silently one day.

### 3. FULL recovery and the first full backup

```powershell
sqlcmd -S localhost -E -I -b -v BACKUP_TARGET=URL BACKUP_ROOT=https://<stshoplite...>.blob.core.windows.net/sqlbackups -i server\backups\02_full_recovery_and_first_backup.sql
```

Progress prints every 10%. At the end: `log chain started`, and the backup's size, compressed size,
and `has_backup_checksums = 1`.

**Check it landed:** portal → container `sqlbackups` → folder `SalesDB` → a `SalesDB_FULL_<timestamp>.bak`
file of about the `compressed_mb` size. (The `credential-test` folder holds the small test file from
step 2; the lifecycle rule removes it after 14 days.)

### Rotating the SAS (before it expires)

Create a new SAS (step 1) and run `01-create-backup-credential.ps1` again: it **replaces** the secret
in the existing credential. Nothing else changes, no job needs editing. Update the expiry in the
runbook.

### Revoking it (if it leaked)

A SAS can't be cancelled on its own: it's valid until it expires or **the key that signed it is
rotated**. Storage account → **Security + networking → Access keys** → **Rotate key** on the key you
noted (key 1). That invalidates every SAS signed with it. Then create a new SAS signed with the other
key and run the credential script again. (Nothing else uses the keys, so rotating one breaks nothing
else.)

---

## Local (Part C1)

Locally the same script writes to a folder inside the container, `TO DISK`; there's no credential and
no Blob Storage.

```bash
docker exec shoplite-sql mkdir -p /var/opt/mssql/backup/SalesDB

SQLCMD='/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -I -b'
docker exec -i shoplite-sql sh -c "$SQLCMD -v BACKUP_TARGET=DISK BACKUP_ROOT=/var/opt/mssql/backup" \
  < server/backups/02_full_recovery_and_first_backup.sql
```

The folder is in the container's data volume, so it survives container restarts (and is deleted with
`docker compose down -v`).

Two local notes:
- **The log now grows until it's backed up.** In FULL recovery, the transaction log is only reused after
  a log backup. B5 adds the local log backup job; until then, heavy local testing grows the log file.
  To go back: `ALTER DATABASE SalesDB SET RECOVERY SIMPLE;`
- **A local rebuild resets this.** `apply_schema.py` recreates SalesDB in SIMPLE recovery; run this
  script again afterwards if you want local backups.
