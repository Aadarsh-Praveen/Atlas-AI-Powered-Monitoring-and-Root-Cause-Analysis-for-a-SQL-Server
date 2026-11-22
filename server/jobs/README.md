# server/jobs

SQL Server's own operations, run by **SQL Server Agent**: backups, the weekly restore test, integrity
checks, index and statistics maintenance, history cleanup, the vendor ETL, and the ReportingDB
refreshes. Plus the server settings and the Extended Events session that records deadlocks, blocking,
slow requests, and errors. The full timetable is **`schedule.md`**.

| File | What it does | Where |
|---|---|---|
| `01-prepare-server.ps1` | `F:\XE` and `F:\Logs\jobs` with the right service accounts; SQL Agent automatic and started | VM only |
| `sql/010_server_settings_and_xe.sql` | Blocked process threshold, backup compression and checksum defaults, the `ShopLite_Diagnostics` session | both (`XE_FILE`) |
| `sql/020_dba_database.sql` | The **DBA** database and the job helper procedures | both |
| *Ola Hallengren's `MaintenanceSolution.sql`* | Downloaded, installed **into DBA** (below); not stored in this repo | both |
| `sql/030_restore_test.sql` | `DBA.dbo.usp_restore_test` and `DBA.dbo.restore_test_runs` | both |
| `sql/040_jobs.sql` | Every job and schedule; safe to re-run (history kept) | both (`BACKUP_TARGET`, `BACKUP_ROOT`, `ETL_REPO_ROOT`) |
| `schedule.md` | The one-page schedule, UTC and Boston | |

**Run them in that order.** Before `040`, these must already be in place: ReportingDB
(`server/reporting/`); SalesDB in FULL recovery with its first full backup (`server/backups/`); in
production also the backup credential (`server/backups/`) and the ETL's virtual environment and `.env`
(`server/etl/`).

## Where results go (what you and Atlas read)

| Question | Look in |
|---|---|
| Did a job run, and did it succeed? | SQL Agent job history (`msdb`), query below |
| What did a backup / index / CHECKDB command do, how long, which error? | `DBA.dbo.CommandLog` (Ola logs every command) |
| Does the latest backup chain actually restore? | `DBA.dbo.restore_test_runs` |
| Did the ETL load the vendor feed? | `SalesDB.ops.etl_runs` |
| Did ReportingDB refresh? | `ReportingDB.ops.refresh_runs` |
| Deadlocks, blocking, slow requests, errors | The `ShopLite_Diagnostics` files (`F:\XE` on the VM) |

No email alerts (no Database Mail, a Phase 4 decision): Phase 5's monitoring and later Atlas watch
these tables.

---

## Install Ola Hallengren's scripts (into DBA)

Ola Hallengren's Maintenance Solution is the industry-standard, free (MIT-licensed) set of procedures
for backups (`DatabaseBackup`), integrity checks (`DatabaseIntegrityCheck`), and index and statistics
maintenance (`IndexOptimize`), all logging to the `CommandLog` table. Official site:
https://ola.hallengren.com.

We install it into **DBA**, not master (its default), so its procedures and CommandLog sit in one
clean database Atlas can read, and we create **our own jobs** (`040`), not Ola's default ones.

**1. Download** `MaintenanceSolution.sql` from https://ola.hallengren.com/scripts/MaintenanceSolution.sql
(Mac: into `~/Downloads`; VM: into `C:\setup`). Don't commit it to the repo.

**2. Edit two things near the top** of the file (in any text editor):

| Find | Change to | Why |
|---|---|---|
| `USE [master]` | `USE [DBA]` | install into DBA |
| `DECLARE @CreateJobs nvarchar(max) = 'Y'` | the same line ending in `'N'` | our jobs come from `040_jobs.sql` |

Leave `@LogToTable` at `'Y'` (the default): that's what creates and fills `CommandLog`. The other
settings there (`@BackupDirectory`, `@CleanupTime`, `@OutputFileDirectory`) only matter for Ola's own
jobs, which we don't create; leave them as they are.

**3. Run it** (after `020`, so DBA exists):

```bash
# Local
SQLCMD='/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -I -b'
docker exec -i shoplite-sql sh -c "$SQLCMD" < ~/Downloads/MaintenanceSolution.sql
```

```powershell
# VM
sqlcmd -S localhost -E -I -b -i C:\setup\MaintenanceSolution.sql
```

**4. Check** it landed in DBA (four rows) and note the version, which is in each procedure's header:

```sql
SELECT name, type_desc FROM DBA.sys.objects
WHERE name IN ('CommandLog', 'DatabaseBackup', 'DatabaseIntegrityCheck', 'IndexOptimize');

SELECT SUBSTRING(OBJECT_DEFINITION(OBJECT_ID('DBA.dbo.DatabaseBackup')),
       CHARINDEX('Version:', OBJECT_DEFINITION(OBJECT_ID('DBA.dbo.DatabaseBackup'))), 30) AS ola_version;
```

Record the version in the runbook. To upgrade later: download, make the same two edits, run again.

---

## Local (Part C1, step 5)

**1. SQL Agent in the container.** `docker-compose.yml` now sets `MSSQL_AGENT_ENABLED`. Apply it (this
recreates the container; the databases are in the volume and stay):

```bash
docker compose up -d
docker compose ps        # wait for "healthy"
```

**2. Scripts in order:**

```bash
SQLCMD='/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -I -b'

docker exec shoplite-sql mkdir -p /var/opt/mssql/xe /var/opt/mssql/backup

docker exec -i shoplite-sql sh -c "$SQLCMD -v XE_FILE=/var/opt/mssql/xe/ShopLite_Diagnostics.xel" \
  < server/jobs/sql/010_server_settings_and_xe.sql
docker exec -i shoplite-sql sh -c "$SQLCMD" < server/jobs/sql/020_dba_database.sql
# ... now install Ola into DBA (above) ...
docker exec -i shoplite-sql sh -c "$SQLCMD" < server/jobs/sql/030_restore_test.sql
docker exec -i shoplite-sql sh -c "$SQLCMD -v BACKUP_TARGET=DISK BACKUP_ROOT=/var/opt/mssql/backup ETL_REPO_ROOT=NONE" \
  < server/jobs/sql/040_jobs.sql
```

`040` ends with a list of the ShopLite jobs and their schedules. Locally there's no ETL job (run the
ETL by hand from the Mac), and the job-history limit message says it isn't supported on Linux: both
expected.

**3. Run each job once by hand** (Part C1 asks for this), then check its result (below):

```sql
EXEC msdb.dbo.sp_start_job N'ShopLite - Backup SalesDB LOG';
EXEC msdb.dbo.sp_start_job N'ShopLite - Backup SalesDB DIFF';
EXEC msdb.dbo.sp_start_job N'ShopLite - Backup SalesDB FULL';
EXEC msdb.dbo.sp_start_job N'ShopLite - Restore test';          -- after the backups above finish
EXEC msdb.dbo.sp_start_job N'ShopLite - Integrity check';
EXEC msdb.dbo.sp_start_job N'ShopLite - Index and statistics maintenance';
EXEC msdb.dbo.sp_start_job N'ShopLite - ReportingDB refresh';
EXEC msdb.dbo.sp_start_job N'ShopLite - ReportingDB full refresh';
EXEC msdb.dbo.sp_start_job N'ShopLite - Backup system, ReportingDB, DBA FULL';
EXEC msdb.dbo.sp_start_job N'ShopLite - History cleanup';
```

`sp_start_job` only **starts** a job and returns at once; give each a moment (the restore test and
CHECKDB take longest) before reading its history. Run them one at a time.

The local backup files appear under `/var/opt/mssql/backup/<server>/<database>/<FULL|DIFF|LOG>/` (Ola's
folder layout): `docker exec shoplite-sql find /var/opt/mssql/backup -name '*.bak' -o -name '*.trn'`.

---

## On prod-sql-01 (Part C3, in the Phase 4 maintenance window)

PowerShell as Administrator, from `C:\atlas`, after B2, B3, and B4's production steps:

```powershell
.\server\jobs\01-prepare-server.ps1

sqlcmd -S localhost -E -I -b -v XE_FILE=F:\XE\ShopLite_Diagnostics.xel -i server\jobs\sql\010_server_settings_and_xe.sql
sqlcmd -S localhost -E -I -b -i server\jobs\sql\020_dba_database.sql
# ... install Ola into DBA (above) ...
sqlcmd -S localhost -E -I -b -i server\jobs\sql\030_restore_test.sql
sqlcmd -S localhost -E -I -b -v BACKUP_TARGET=URL BACKUP_ROOT=https://<stshoplite...>.blob.core.windows.net/sqlbackups ETL_REPO_ROOT=C:\atlas -i server\jobs\sql\040_jobs.sql
```

Then, as Part C3 asks: run the ETL job, the ReportingDB refresh, and a log backup by hand
(`sp_start_job` as above), check their results, and check the log backup's `.trn` file appears in the
`sqlbackups` container. The first **restore test by hand** (Part C3, step 6): run the restore test job
and time it; `restore_seconds` in `restore_test_runs` is your measured RTO.

---

## Reading the results

**Last run of every ShopLite job:**

```sql
WITH last_run AS (
    SELECT h.job_id, h.run_status, h.run_duration, msdb.dbo.agent_datetime(h.run_date, h.run_time) AS ran_at,
           h.message, ROW_NUMBER() OVER (PARTITION BY h.job_id ORDER BY h.instance_id DESC) AS n
    FROM msdb.dbo.sysjobhistory AS h
    WHERE h.step_id = 0                                    -- the job's overall outcome
)
SELECT j.name, r.ran_at,
       CASE r.run_status WHEN 1 THEN 'succeeded' WHEN 0 THEN 'FAILED' WHEN 3 THEN 'cancelled' ELSE 'other' END AS outcome,
       r.run_duration AS hhmmss, LEFT(r.message, 200) AS message
FROM msdb.dbo.sysjobs AS j
JOIN msdb.dbo.syscategories AS c ON c.category_id = j.category_id AND c.name = 'ShopLite'
LEFT JOIN last_run AS r ON r.job_id = j.job_id AND r.n = 1
ORDER BY j.name;
```

A job with no row hasn't run yet. For a failed job, the **step** row has the full error:
`SELECT TOP 5 * FROM msdb.dbo.sysjobhistory WHERE step_id > 0 ORDER BY instance_id DESC;`

**What Ola did** (newest first; `ErrorNumber` 0 = fine):

```sql
SELECT TOP 20 StartTime, DATEDIFF(SECOND, StartTime, EndTime) AS seconds, DatabaseName, CommandType,
       ErrorNumber, LEFT(ErrorMessage, 200) AS error
FROM DBA.dbo.CommandLog ORDER BY ID DESC;
```

**Restore tests:**

```sql
SELECT TOP 5 * FROM DBA.dbo.restore_test_runs ORDER BY run_id DESC;
```

**Extended Events:** counts per event type over the files (local path shown; on the VM
`F:\XE\ShopLite_Diagnostics*.xel`):

```sql
SELECT object_name AS event, COUNT(*) AS events, MIN(timestamp_utc) AS first, MAX(timestamp_utc) AS last
FROM sys.fn_xe_file_target_read_file(N'/var/opt/mssql/xe/ShopLite_Diagnostics*.xel', NULL, NULL, NULL)
GROUP BY object_name;
```

`event_data` holds each event as XML (the deadlock graph, the blocked process report, the slow
statement). SSMS and Azure Data Studio can open `.xel` files directly too.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No job ran today at all | SQL Agent isn't running: `01-prepare-server.ps1` (VM) or `MSSQL_AGENT_ENABLED` (local) |
| Backup jobs fail with "Operating system error 86" or "403" on the URL | The SAS expired or was revoked: new SAS, re-run `01-create-backup-credential.ps1` |
| Log backup fails: "no current database backup" | The chain was broken (SIMPLE recovery, or a rebuild). The log job switches to a full by itself (`@ChangeBackupType`); or take one by hand |
| Restore test: "The log chain is broken" | A log backup is missing or failed (check the LOG job's history at that time), or someone switched SalesDB to SIMPLE. The next full backup starts a new chain |
| Restore test: CHECKDB errors | Possible corruption in the backup **or** in SalesDB: run the integrity check job on SalesDB now, and keep every backup (don't let anything overwrite them) |
| ETL job fails at once with exit code 2 or 3 | The Agent account can't read `server\etl\.env`, or a setting is wrong: `F:\Logs\jobs\etl.log` has the message |
| Index maintenance runs past 23:00 | It shouldn't (30-minute limit); if it does, a single large rebuild started late: check CommandLog |
| `040` says Ola's procedures aren't in DBA | Ola was installed into master (the `USE` line wasn't changed): install again into DBA |
