# server/collector

The **monitoring collector**: a small Python service on prod-sql-01 that reads SQL Server's health
and ShopLite's operations every minute and sends them to **Log Analytics** (31 days, alerts) and
**ADX** (12 months, Grafana and forecasting). What it collects, table by table, is in
**`observability/schema.yml`**: waits, performance counters, sessions, blocking, Extended Events,
database sizes and backup ages, file I/O, top queries, job runs, backups, ETL runs, refreshes,
restore tests, maintenance commands, the vendor's freshness, and its own heartbeat.

It runs as a **Windows service, not a SQL Agent job**: monitoring must not depend on what it
monitors. When SQL Server is down, the collector keeps running and its heartbeat says so.

## Files

| File | What it is |
|---|---|
| `sql/001_monitor_login.sql` | The `shoplite_monitor` login: reads health information and the operational tables, nothing else |
| `sql_queries.py` | Every T-SQL query, with the permission each needs |
| `collect.py` | Runs the collections on their schedules (1, 5, 15 min) and builds the records |
| `xevents.py` | Reads new events from the Extended Events files |
| `buffer.py` | The disk buffer: records wait here until their destination accepts them |
| `senders.py` | Log Analytics (managed identity), ADX (app registration), and print |
| `main.py` | The service loop: collect → store → commit → send → heartbeat |
| `.env.example` | Settings template |
| `requirements.txt` | Its packages, in its own `.venv` |
| `tests/test_collector.py` | Tests against local SQL Server, including every query as `shoplite_monitor` |
| `deploy/shoplite-collector.xml` | The WinSW service definition |

## How it works

Every minute, `main.py`:
1. **collects** what's due: every minute waits, counters, sessions, blocking, and Extended Events;
   every 5 minutes databases, file I/O, and the event tables; every 15 minutes Query Store and the
   vendor;
2. **stores** the records in each destination's **buffer** on disk (`F:\collector\buffer`);
3. **commits** the watermarks (which events have been collected), only now that the records are safe;
4. **sends** each destination's buffered files, oldest first. A failure leaves the files and backs
   off that destination (1 minute, doubling, at most 15);
5. writes a **heartbeat** row: cycle time, failed collections and the last error, `SqlOk`,
   `LogAnalyticsOk`, `AdxOk`, and how many records are waiting.

**At-least-once delivery:** after a crash or a failed send, a batch can arrive twice, never zero
times. Every event table has a `unique_id` column (schema.yml) to filter duplicates when querying:
`OpsEtlRuns_CL | summarize arg_max(TimeGenerated, *) by RunId`.

**Light on the server:** 15-second query timeouts, `READ UNCOMMITTED` for the history tables (it
never blocks a writer), `LOCK_TIMEOUT 5000`, and its sessions are tagged `ShopLite-Collector`.

**Sessions aren't filtered:** the collector reports every session exactly as SQL Server shows it,
including the incident injector's (which use realistic names; Phase 5 A5).

---

## Local (Part C1, step 1)

Needs Phase 4 set up locally: SalesDB, ReportingDB, DBA with Ola, the Extended Events session.

**1. The monitor login.** Choose a password (SQL Server's policy; only `- _ . ~` as symbols):

```bash
read -rs SHOPLITE_MONITOR_PASSWORD && export SHOPLITE_MONITOR_PASSWORD
docker exec -i -e MP="$SHOPLITE_MONITOR_PASSWORD" shoplite-sql sh -c \
  '/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -I -b -v SHOPLITE_MONITOR_PASSWORD="$MP"' \
  < server/collector/sql/001_monitor_login.sql
```

**2. Virtual environment and settings:**

```bash
python3 -m venv server/collector/.venv
server/collector/.venv/bin/pip install -r server/collector/requirements.txt "pytest>=8.3,<9"
# Settings: nothing to copy. The root .env needs SHOPLITE_MONITOR_PASSWORD (and, for the vendor
# check, SHOPLITE_READER_PASSWORD); the XE path has a local default; destinations stay empty.
```

**3. One cycle, printed** (sends nothing, saves no watermarks, so you can repeat it):

```bash
cd server/collector && .venv/bin/python main.py --print --once && cd -
```

Every table with rows prints its row count and first row.

**`--once` never shows the delta tables.** `SqlWaitStats_CL`, `SqlFileIo_CL`, and the per-second
counters in `SqlPerfCounters_CL` (Batch Requests/sec, Lock Waits/sec, ...) are *differences* between
two snapshots, and the previous snapshot is kept in memory only. A single cycle has nothing to
compare with, and a new process starts with an empty memory. To see them, let one process run
several cycles:

```bash
cd server/collector && .venv/bin/python main.py --print && cd -     # Ctrl+C to stop
```

| Table | First appears |
|---|---|
| `SqlWaitStats_CL`, per-second counters in `SqlPerfCounters_CL` | 2nd cycle, after about 1 minute |
| `SqlFileIo_CL` | 2nd run of the 5-minute collections, after about 5–6 minutes |
| `SqlTopQueries_CL`, `VendorFeed_CL` | 1st cycle (no deltas), then every 15 minutes |

So about 6 minutes of `--print` shows every table. Some tables only have rows when something
happened: blocking, new Extended Events, finished jobs, backups, ETL runs. (`--print` still saves no
watermarks, so each new run starts from the last 24 hours again.)

**4. Tests:**

```bash
server/collector/.venv/bin/python -m pytest server/collector/tests -v
```

| Test group | What a failure means |
|---|---|
| `test_query_runs_as_monitor[...]` | That query fails as `shoplite_monitor`. A **permission** error names the query: see "The permission question" below |
| `test_monitor_holds_the_narrow_server_permission` | The login has `VIEW SERVER STATE`: re-run the login script |
| `test_monitor_cannot_touch_business_data` | A grant is too broad |
| `test_cycles_run_without_failures`, `test_every_record_matches_the_schema` | A collection fails, or a record doesn't match `schema.yml` (the message lists the column) |
| `test_blocking_chain_is_reported` | The blocking collection misses a chain it should see (uses the local `sa` from the root `.env`, only to create the chain) |
| offline tests | The XE parser, the buffer, or the ADX script vs the schema |

### The permission question (settled)

The login uses SQL Server 2022's narrower **`VIEW SERVER PERFORMANCE STATE`** instead of `VIEW SERVER
STATE` (decided with the owner).

**Result, 2026-11-22 (local SQL Server 2022):** every `test_query_runs_as_monitor` case passed as
`shoplite_monitor`: every collector query, including the per-database ones in master, tempdb, msdb,
SalesDB, ReportingDB, and DBA. **`VIEW SERVER PERFORMANCE STATE` is sufficient; no query needs `VIEW
SERVER STATE`.** The collector then ran 10 minutes with no failed collections.

If a future query ever fails with a permission error, the rule stays the same: the login goes back to
`VIEW SERVER STATE` only if the narrower permission really can't cover it, and `001_monitor_login.sql`
names that query and the reason.

---

## On prod-sql-01 (Part C3, step 2)

PowerShell as Administrator, from `C:\atlas` after pulling. Needs: the Azure resources from
`observability/azure-setup.md` (B3) for the destinations' settings.

**1. The login** (password asked for, not typed on the command line):

```powershell
$secure = Read-Host "shoplite_monitor password" -AsSecureString
$plain = [System.Net.NetworkCredential]::new('', $secure).Password
sqlcmd -S localhost -E -I -b -v SHOPLITE_MONITOR_PASSWORD="$plain" -i server\collector\sql\001_monitor_login.sql
Remove-Variable plain, secure
```

**2. Virtual environment:**

```powershell
py -3.12 -m venv server\collector\.venv
server\collector\.venv\Scripts\pip install -r server\collector\requirements.txt
```

**3. Settings, locked down.** Production values: `XE_FILE_PATTERN=F:\XE\ShopLite_Diagnostics*.xel`,
`COLLECTOR_DATA_DIR=F:\collector`, the vendor URL (Neon, `?sslmode=require`), and the Log Analytics and
ADX values from the setup guide:

```powershell
Copy-Item server\collector\.env.example server\collector\.env
notepad server\collector\.env
icacls C:\atlas\server\collector\.env /inheritance:r /grant:r "Administrators:F" "SYSTEM:F" "NT AUTHORITY\LocalService:R"
```

The template starts with `LOAD_ROOT_ENV=false`: on the VM this file is the collector's only source.
**Already have this file from before (it predates layered settings)?** Add the line `LOAD_ROOT_ENV=false` to it, so the root `.env` is never read even when you run this by hand as Administrator.

**4. Folders for its state, buffer, and logs:**

```powershell
New-Item -ItemType Directory -Force F:\collector, F:\Logs\collector | Out-Null
icacls F:\collector       /grant "NT AUTHORITY\LocalService:(OI)(CI)M"
icacls F:\Logs\collector  /grant "NT AUTHORITY\LocalService:(OI)(CI)M"
```

**5. Try one cycle by hand** (as Administrator; it uses the same `.env`):

```powershell
cd C:\atlas\server\collector
.\.venv\Scripts\python.exe main.py --once
```

The log line says how many rows and failed collections. `LogAnalyticsOk`/`AdxOk` problems show as
warnings (`send failed ...`).

**6. Install the service** (WinSW v2.12.0, the same `WinSW-x64.exe` as the API):

```powershell
New-Item -ItemType Directory -Force C:\services\shoplite-collector | Out-Null
Copy-Item $HOME\Downloads\WinSW-x64.exe C:\services\shoplite-collector\shoplite-collector.exe
Copy-Item C:\atlas\server\collector\deploy\shoplite-collector.xml C:\services\shoplite-collector\shoplite-collector.xml
C:\services\shoplite-collector\shoplite-collector.exe install
Start-Service shoplite-collector
Get-Content F:\Logs\collector\shoplite-collector.out.log -Tail 20 -Wait     # one line a minute
```

**7. Check data arrives** (setup guide, "Check that data arrives"):
`CollectorHeartbeat_CL | top 5 by TimeGenerated desc` in Log Analytics, and the same query in ADX.

### Changing the service definition

`git pull`, then:

```powershell
C:\services\shoplite-collector\shoplite-collector.exe stop
C:\services\shoplite-collector\shoplite-collector.exe uninstall
Copy-Item C:\atlas\server\collector\deploy\shoplite-collector.xml C:\services\shoplite-collector\shoplite-collector.xml
C:\services\shoplite-collector\shoplite-collector.exe install
Start-Service shoplite-collector
```

A code change (no XML change) only needs `git pull` and `Restart-Service shoplite-collector`.

---

## Check the monitor login

As `shoplite_monitor` (the tests do this too):

```sql
SELECT TOP 1 * FROM SalesDB.dbo.orders;        -- must fail: permission was denied
SELECT COUNT(*) FROM sys.dm_exec_sessions;     -- works: sees every session
SELECT permission_name FROM sys.fn_my_permissions(NULL, 'SERVER');   -- VIEW SERVER PERFORMANCE STATE, not VIEW SERVER STATE
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No heartbeat in Log Analytics | Service stopped (`Get-Service shoplite-collector`), or every send fails: the log shows `send failed` with the reason |
| `SqlOk = false`, LastError "Login failed for user 'shoplite_monitor'" | `SQL_PASSWORD` in `.env` doesn't match the login (re-run the login script with the same value) |
| `QueriesFailed > 0` | `FailedCollections` names every failure (e.g. `top_queries, databases[master]`); the log has a `collection ... failed:` line with each full message. Usual causes: a timeout under heavy load, a missing permission, a database being restored |
| `LogAnalyticsOk = false`, 403 | The VM's managed identity lacks "Monitoring Metrics Publisher" on the data collection rules (setup guide) |
| `LogAnalyticsOk = false`, 404 or "stream" errors | Wrong DCE URL or DCR immutable ID, or a custom table/stream is missing |
| `AdxOk = false`, 401/403 | The app registration's secret expired or it lacks the `ingestor` role on the database |
| `BufferedRecords` keeps growing | A destination is unreachable; data is safe on `F:` until the buffer's age or size limit (warnings in the log) |
| No `SqlXEvents_CL` rows | No events yet (normal on a quiet server), or `XE_FILE_PATTERN` doesn't match the session's files |
| Duplicate rows | Expected now and then (at-least-once); filter by the table's `unique_id` |
