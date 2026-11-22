"""Every T-SQL query the monitoring collector runs, in one place.

The collector connects as shoplite_monitor (sql/001_monitor_login.sql). Each
query says what it reads and which of that login's permissions it relies on;
the local tests (tests/) run every one of them as shoplite_monitor, so a
missing permission shows up there, not in production.

Conventions:
  - Read-only, short, and cheap: DMVs are in-memory views; the history tables
    are read through their keys (watermarks), never scanned whole.
  - Parameters are "?" (pyodbc); database names, which can't be parameters,
    come only from the collector's own configured list, bracket-quoted.
  - SQL Server's own times (sysdatetime, msdb dates, ops.* columns) are the
    server's local time; the collector converts them to UTC using
    SERVER_INFO's offset (0 on the VM and in the container, which run in UTC).
  - Every query below is ONE statement that returns rows (a SELECT, or a
    WITH ... SELECT). Nothing before it in the same batch: no USE, SET, or
    DECLARE, whose empty results would come first and make pyodbc's fetchall()
    fail with "No results. Previous SQL was not a query." Always run them
    through run_query() below, which also enforces this. (SESSION_SETUP is the
    one exception: it's run once, never fetched.)
"""

import pyodbc


def run_query(cursor, sql: str, *params, database: str | None = None) -> list:
    """Run one query and return its rows.

    database: run it inside that database. The USE goes in its OWN execute()
    first (it changes the connection's current database and returns no rows),
    so the query itself is the batch's only statement.

    Safety net: if the batch still starts with something that returns no rows
    (a row count, a SET), skip those results with nextset() until the first
    real result set; if there's none at all, fail with a clear message."""
    if database:
        cursor.execute("USE [" + database.replace("]", "]]") + "];")
    cursor.execute(sql, *params)
    while cursor.description is None:        # this result has no columns: not the rows we want
        if not cursor.nextset():
            first_line = sql.strip().splitlines()[0][:80]
            raise pyodbc.ProgrammingError(f"The query returned no result set: {first_line}")
    return cursor.fetchall()

# -----------------------------------------------------------------------------
# Session settings, run once per connection.
#   READ UNCOMMITTED: reading msdb and the ops tables never waits behind a
#     writer and never blocks one. DMVs ignore isolation levels anyway. Safe
#     here: the collector reads only FINISHED rows (finished_at set, outcome
#     rows), which are written in a single statement and never changed again.
#   LOCK_TIMEOUT 5000: if something still has to wait for a lock, give up
#     after 5 seconds rather than hang the cycle.
# -----------------------------------------------------------------------------
SESSION_SETUP = """
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
SET LOCK_TIMEOUT 5000;
SET NOCOUNT ON;
"""

# -----------------------------------------------------------------------------
# Server identity, restart time, and clock offset.
# Needs: VIEW SERVER PERFORMANCE STATE (sys.dm_os_sys_info).
# sqlserver_start_time changes when SQL Server restarts: the collector then
# throws away its previous counters, because every cumulative DMV restarted at 0.
# -----------------------------------------------------------------------------
SERVER_INFO = """
SELECT CAST(SERVERPROPERTY('MachineName') AS NVARCHAR(128)) AS machine_name,
       i.sqlserver_start_time,
       DATEDIFF(MINUTE, SYSUTCDATETIME(), SYSDATETIME()) AS utc_offset_minutes
FROM sys.dm_os_sys_info AS i;
"""

# =============================================================================
# Every minute
# =============================================================================

# Cumulative wait statistics since SQL Server started; the collector keeps the
# previous snapshot and sends the difference. Harmless background waits are
# filtered out in collect.py (one readable list there, not here).
# Needs: VIEW SERVER PERFORMANCE STATE.
WAIT_STATS = """
SELECT wait_type, waiting_tasks_count, wait_time_ms, signal_wait_time_ms
FROM sys.dm_os_wait_stats
WHERE wait_time_ms > 0;
"""

# Performance counters. cntr_type 272696576 = a cumulative count the collector
# turns into a per-second rate; 65792 = a current value. Which counters (and
# which instances) to keep is decided in collect.py.
# Needs: VIEW SERVER PERFORMANCE STATE.
PERF_COUNTERS = """
SELECT RTRIM(object_name) AS object_name, RTRIM(counter_name) AS counter_name,
       RTRIM(instance_name) AS instance_name, cntr_value, cntr_type
FROM sys.dm_os_performance_counters
WHERE counter_name IN (
    N'Batch Requests/sec', N'SQL Compilations/sec', N'SQL Re-Compilations/sec',
    N'User Connections', N'Processes blocked', N'Lock Waits/sec',
    N'Number of Deadlocks/sec', N'Page life expectancy', N'Memory Grants Pending',
    N'Total Server Memory (KB)', N'Target Server Memory (KB)',
    N'Transactions/sec', N'Log Flushes/sec', N'Percent Log Used');
"""

# User sessions, grouped. LEFT JOIN requests: a session without a request is
# idle (it may still hold an open transaction, counted separately).
# start_time is server-local time, like SYSDATETIME().
# Needs: VIEW SERVER PERFORMANCE STATE (to see sessions other than its own).
SESSIONS = """
SELECT DB_NAME(s.database_id)                                      AS database_name,
       s.program_name, s.login_name, s.host_name,
       COUNT(*)                                                    AS sessions,
       SUM(CASE WHEN r.session_id IS NOT NULL THEN 1 ELSE 0 END)   AS running_requests,
       SUM(CASE WHEN r.blocking_session_id > 0 THEN 1 ELSE 0 END)  AS blocked_requests,
       SUM(CASE WHEN s.open_transaction_count > 0 THEN 1 ELSE 0 END) AS open_transaction_sessions,
       MAX(DATEDIFF_BIG(MILLISECOND, r.start_time, SYSDATETIME())) / 1000.0 AS longest_request_seconds
FROM sys.dm_exec_sessions AS s
LEFT JOIN sys.dm_exec_requests AS r ON r.session_id = s.session_id
WHERE s.is_user_process = 1
GROUP BY DB_NAME(s.database_id), s.program_name, s.login_name, s.host_name;
"""

# Blocking, step 1: every request waiting on another session (positive IDs
# only: negative ones are special cases such as orphaned distributed
# transactions). The chains and their heads are worked out in collect.py.
# Needs: VIEW SERVER PERFORMANCE STATE.
BLOCKED_REQUESTS = """
SELECT session_id, blocking_session_id, wait_type, wait_time AS wait_time_ms, wait_resource
FROM sys.dm_exec_requests
WHERE blocking_session_id > 0 AND blocking_session_id <> session_id;
"""

# Blocking, step 2: details of every session in the chains (blocked ones and
# head blockers). The statement is the one running now; for an idle head
# blocker (sleeping, transaction still open) it's the last one it ran,
# from its connection. The oldest open transaction's start shows how long
# it has held its locks. One parameter: comma-separated session IDs.
# Needs: VIEW SERVER PERFORMANCE STATE (sessions, requests, connections,
# transaction DMVs, sys.dm_exec_sql_text for other sessions).
SESSION_DETAILS = """
SELECT s.session_id, DB_NAME(s.database_id) AS database_name, s.program_name, s.login_name,
       s.host_name, s.status, s.open_transaction_count,
       (SELECT MIN(at.transaction_begin_time)
        FROM sys.dm_tran_session_transactions AS st
        JOIN sys.dm_tran_active_transactions AS at ON at.transaction_id = st.transaction_id
        WHERE st.session_id = s.session_id)                       AS oldest_transaction_begin,
       LEFT(COALESCE(current_stmt.text, last_batch.text), 4000)   AS statement_text
FROM sys.dm_exec_sessions AS s
LEFT JOIN sys.dm_exec_requests AS r ON r.session_id = s.session_id
LEFT JOIN sys.dm_exec_connections AS c ON c.session_id = s.session_id
OUTER APPLY (
    SELECT SUBSTRING(t.text, r.statement_start_offset / 2 + 1,
               (CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(t.text)
                     ELSE r.statement_end_offset END - r.statement_start_offset) / 2 + 1) AS text
    FROM sys.dm_exec_sql_text(r.sql_handle) AS t) AS current_stmt
OUTER APPLY (SELECT t.text FROM sys.dm_exec_sql_text(c.most_recent_sql_handle) AS t) AS last_batch
WHERE s.session_id IN (SELECT CAST(value AS INT) FROM STRING_SPLIT(?, ','));
"""

# New Extended Events since a file position. sys.fn_xe_file_target_read_file
# returns the events after (initial_file_name, initial_offset); with both NULL
# it reads every file matching the pattern (first start, or after the file
# rolled over). timestamp_utc lets the first start skip old events.
# Parameters: file pattern, initial file name, initial offset.
# Needs: VIEW SERVER PERFORMANCE STATE.
XE_EVENTS = """
SELECT object_name, CAST(event_data AS NVARCHAR(MAX)) AS event_data, file_name, file_offset,
       timestamp_utc
FROM sys.fn_xe_file_target_read_file(?, NULL, ?, ?);
"""

# =============================================================================
# Every 5 minutes
# =============================================================================

# Databases: state, recovery model, log reuse wait, last normal backups, for
# EVERY database (these come from server-level views anyone can read).
# HAS_DBACCESS(name) = 1 means the login can enter the database: the
# per-database queries below (space, file I/O) run only there. Which ones:
#   SalesDB, ReportingDB, DBA, msdb   a shoplite_monitor user (001_monitor_login.sql)
#   master, tempdb                    the built-in guest user, enabled in both by
#                                     default. tempdb is kept on purpose: its size
#                                     and I/O are a classic trouble spot (sorts,
#                                     spills, version store).
#   model                             no user and guest is off: skipped, which is
#                                     right (nothing runs in it; it's the template
#                                     for new databases)
# A database the login can't enter still gets its row, just without space
# numbers, and that's not counted as a failure. The same goes for a
# SalesDB_RestoreTest that exists for a few minutes during the weekly restore
# test (State RESTORING).
# Needs: sys.databases (visible to every login), SELECT on msdb.dbo.backupset.
DATABASES = """
SELECT d.name, d.state_desc, d.recovery_model_desc, d.log_reuse_wait_desc,
       HAS_DBACCESS(d.name) AS has_access,
       (SELECT MAX(b.backup_finish_date) FROM msdb.dbo.backupset AS b
        WHERE b.database_name = d.name AND b.type = 'D' AND b.is_copy_only = 0) AS last_full,
       (SELECT MAX(b.backup_finish_date) FROM msdb.dbo.backupset AS b
        WHERE b.database_name = d.name AND b.type = 'I' AND b.is_copy_only = 0) AS last_diff,
       (SELECT MAX(b.backup_finish_date) FROM msdb.dbo.backupset AS b
        WHERE b.database_name = d.name AND b.type = 'L' AND b.is_copy_only = 0) AS last_log
FROM sys.databases AS d;
"""

# Space inside ONE database, run in that database (the collector prefixes
# "USE [name];"). Data size from sys.database_files (visible to any user of
# the database), space used via FILEPROPERTY, log usage from
# sys.dm_db_log_space_usage. Sizes are in 8 KB pages.
# Needs: a user in the database (or guest: master, tempdb) and
# VIEW DATABASE STATE / VIEW SERVER PERFORMANCE STATE for the log DMV.
DATABASE_SPACE = """
SELECT
    (SELECT SUM(CAST(size AS BIGINT)) * 8 / 1024.0 FROM sys.database_files WHERE type = 0)
        AS data_size_mb,
    (SELECT SUM(CAST(FILEPROPERTY(name, 'SpaceUsed') AS BIGINT)) * 8 / 1024.0
     FROM sys.database_files WHERE type = 0)                       AS data_used_mb,
    l.total_log_size_in_bytes / 1048576.0                          AS log_size_mb,
    l.used_log_space_in_percent                                    AS log_used_percent
FROM sys.dm_db_log_space_usage AS l;
"""

# File I/O for ONE database, run in that database. Joined to
# sys.database_files for the file names, rather than sys.master_files, whose
# rows are hidden from logins without VIEW ANY DEFINITION (a permission the
# collector doesn't need otherwise). Cumulative since restart: deltas in collect.py.
# Needs: a user in the database, VIEW SERVER PERFORMANCE STATE.
DATABASE_FILE_IO = """
SELECT f.name AS file_name, f.type_desc,
       vfs.num_of_reads, vfs.num_of_writes, vfs.num_of_bytes_read, vfs.num_of_bytes_written,
       vfs.io_stall_read_ms, vfs.io_stall_write_ms
FROM sys.dm_io_virtual_file_stats(DB_ID(), NULL) AS vfs
JOIN sys.database_files AS f ON f.file_id = vfs.file_id;
"""

# Finished job runs after a watermark (instance_id grows with every history
# row). step_id 0 is the job's outcome row; for a failed run, the latest
# failed step of the same run (between the previous outcome row and this one)
# gives the step name and its error. agent_datetime turns msdb's integer
# date and time into a datetime (server-local).
# Parameters: last instance_id seen, earliest start time (first start only).
# Needs: SELECT on msdb.dbo.sysjobhistory, sysjobs, syscategories;
# EXECUTE on msdb.dbo.agent_datetime.
JOB_RUNS = """
SELECT h.instance_id, j.name AS job_name, c.name AS category, h.run_status,
       msdb.dbo.agent_datetime(h.run_date, h.run_time) AS started_at,
       h.run_duration, LEFT(h.message, 2000) AS message,
       fs.step_name AS failed_step_name, LEFT(fs.message, 2000) AS failed_step_message
FROM msdb.dbo.sysjobhistory AS h
JOIN msdb.dbo.sysjobs AS j ON j.job_id = h.job_id
JOIN msdb.dbo.syscategories AS c ON c.category_id = j.category_id
OUTER APPLY (
    SELECT TOP 1 s.step_name, s.message
    FROM msdb.dbo.sysjobhistory AS s
    WHERE s.job_id = h.job_id AND s.step_id > 0 AND s.run_status = 0
      AND s.instance_id < h.instance_id
      AND s.instance_id > ISNULL((SELECT MAX(p.instance_id) FROM msdb.dbo.sysjobhistory AS p
                                  WHERE p.job_id = h.job_id AND p.step_id = 0
                                    AND p.instance_id < h.instance_id), 0)
    ORDER BY s.instance_id DESC) AS fs
WHERE h.step_id = 0 AND h.instance_id > ?
  AND msdb.dbo.agent_datetime(h.run_date, h.run_time) >= ?
ORDER BY h.instance_id;
"""

# Finished backups after a watermark (a backupset row is written when the
# backup completes). family_sequence_number 1: the first (here, only) file.
# Parameters: last backup_set_id seen, earliest finish time (first start only).
# Needs: SELECT on msdb.dbo.backupset and backupmediafamily.
BACKUPS = """
SELECT b.backup_set_id, b.database_name, b.type, b.backup_start_date, b.backup_finish_date,
       b.backup_size / 1048576.0 AS size_mb, b.compressed_backup_size / 1048576.0 AS compressed_mb,
       b.is_copy_only, m.physical_device_name
FROM msdb.dbo.backupset AS b
JOIN msdb.dbo.backupmediafamily AS m ON m.media_set_id = b.media_set_id AND m.family_sequence_number = 1
WHERE b.backup_set_id > ? AND b.backup_finish_date >= ?
ORDER BY b.backup_set_id;
"""

# The operational tables: rows that FINISHED after a watermark (a finish
# time, not an ID: a long run with a lower ID can finish after a short one).
# Parameter: the last finish time seen (first start: 24 hours ago).
# Needs: SELECT on each table (granted by 001_monitor_login.sql).
ETL_RUNS = """
SELECT run_id, feed, contract_version, started_at, finished_at, status,
       rows_read, rows_skipped, rows_inserted, rows_updated, rows_discontinued, rows_unchanged,
       vendor_newest_price_at, error_stage, LEFT(error_message, 2000) AS error_message
FROM SalesDB.ops.etl_runs
WHERE finished_at > ? AND status <> 'running'
ORDER BY finished_at;
"""

REFRESH_RUNS = """
SELECT run_id, mode, from_date, cutoff_at, started_at, finished_at, status,
       sale_lines, days_rebuilt, rows_written, LEFT(error_message, 2000) AS error_message
FROM ReportingDB.ops.refresh_runs
WHERE finished_at > ? AND status <> 'running'
ORDER BY finished_at;
"""

RESTORE_TESTS = """
SELECT run_id, started_at, finished_at, status, full_backup_finished_at, log_backups_restored,
       restored_to, restore_seconds, checkdb_seconds, LEFT(error_message, 2000) AS error_message
FROM DBA.dbo.restore_test_runs
WHERE finished_at > ? AND status <> 'running'
ORDER BY finished_at;
"""

# Ola's CommandLog: a row gets EndTime when its command finishes.
COMMAND_LOG = """
SELECT ID, DatabaseName, CommandType,
       CASE WHEN ObjectName IS NULL THEN NULL ELSE CONCAT(SchemaName, N'.', ObjectName) END AS object_name,
       IndexName, StartTime, EndTime, ErrorNumber, LEFT(ErrorMessage, 2000) AS error_message,
       LEFT(Command, 2000) AS command
FROM DBA.dbo.CommandLog
WHERE EndTime > ?
ORDER BY EndTime;
"""

# =============================================================================
# Every 15 minutes
# =============================================================================

# Query Store intervals that have ENDED since the last one sent, in ONE
# database (run there; "USE [name];" prefix). Each interval is sent once.
# Query Store's times are datetimeoffset, a type pyodbc can't read, so they're
# converted to plain UTC datetime2 here (SWITCHOFFSET to +00:00, then CONVERT).
# Parameter: the last interval end sent (UTC datetime).
# Needs: VIEW DATABASE STATE in the database.
QUERY_STORE_INTERVALS = """
SELECT runtime_stats_interval_id,
       CONVERT(DATETIME2(0), SWITCHOFFSET(start_time, '+00:00')) AS start_utc,
       CONVERT(DATETIME2(0), SWITCHOFFSET(end_time, '+00:00'))   AS end_utc
FROM sys.query_store_runtime_stats_interval
WHERE end_time > TODATETIMEOFFSET(CAST(? AS DATETIME2), '+00:00')
  AND end_time <= SYSDATETIMEOFFSET()
ORDER BY end_time;
"""

# The top 25 (query, plan) pairs by total duration in ONE interval. Query
# Store stores averages per interval; average x executions gives totals,
# which add up correctly in the hourly rollups. Times are microseconds in
# Query Store, milliseconds here. plans_for_query counts the plans each query
# used in this interval, computed before TOP so it sees every plan.
# Parameter: runtime_stats_interval_id.
# Needs: VIEW DATABASE STATE in the database.
QUERY_STORE_TOP = """
WITH per_plan AS (
    SELECT p.query_id, rs.plan_id,
           SUM(rs.count_executions)                                AS executions,
           SUM(rs.avg_duration * rs.count_executions) / 1000.0     AS total_duration_ms,
           SUM(rs.avg_cpu_time * rs.count_executions) / 1000.0     AS total_cpu_ms,
           SUM(rs.avg_logical_io_reads * rs.count_executions)      AS total_logical_reads,
           MAX(rs.max_duration) / 1000.0                           AS max_duration_ms
    FROM sys.query_store_runtime_stats AS rs
    JOIN sys.query_store_plan AS p ON p.plan_id = rs.plan_id
    WHERE rs.runtime_stats_interval_id = ?
    GROUP BY p.query_id, rs.plan_id
)
SELECT TOP (25)
       pp.query_id, pp.plan_id, pp.executions, pp.total_duration_ms, pp.total_cpu_ms,
       pp.total_logical_reads, pp.max_duration_ms,
       COUNT(*) OVER (PARTITION BY pp.query_id)                    AS plans_for_query,
       CONVERT(VARCHAR(18), q.query_hash, 1)                       AS query_hash,
       CONVERT(VARCHAR(18), p.query_plan_hash, 1)                  AS plan_hash,
       p.is_forced_plan                                            AS is_forced_plan,
       LEFT(t.query_sql_text, 2000)                                AS query_text
FROM per_plan AS pp
JOIN sys.query_store_query AS q ON q.query_id = pp.query_id
JOIN sys.query_store_query_text AS t ON t.query_text_id = q.query_text_id
JOIN sys.query_store_plan AS p ON p.plan_id = pp.plan_id
ORDER BY pp.total_duration_ms DESC;
"""

# Which databases to read Query Store from: Query Store on (it could be
# switched off, or full and read-only, which still has data worth reading),
# online, AND enterable by this login. That last condition matters: in SQL
# Server 2022 model has Query Store on (new databases inherit it from model),
# but the collector can't enter model, so without HAS_DBACCESS it would try
# and fail with error 916 every 15 minutes.
# Needs: sys.databases.
QUERY_STORE_DATABASES = """
SELECT name FROM sys.databases
WHERE is_query_store_on = 1 AND state_desc = N'ONLINE' AND HAS_DBACCESS(name) = 1;
"""

# -----------------------------------------------------------------------------
# The vendor (Postgres, as the read-only shoplite_reader; not SQL Server).
# -----------------------------------------------------------------------------
VENDOR_FRESHNESS = """
SELECT max(p.updated_at) AS newest_price_at, now() AS vendor_now,
       (SELECT count(*) FROM supply.catalog_items) AS catalog_items
FROM supply.prices AS p
"""

# Every SQL Server query above, for the permission tests: name -> text. The
# per-database ones are listed separately because they run inside a database.
SERVER_QUERIES = {
    "SERVER_INFO": SERVER_INFO, "WAIT_STATS": WAIT_STATS, "PERF_COUNTERS": PERF_COUNTERS,
    "SESSIONS": SESSIONS, "BLOCKED_REQUESTS": BLOCKED_REQUESTS, "DATABASES": DATABASES,
    "QUERY_STORE_DATABASES": QUERY_STORE_DATABASES,
}
PER_DATABASE_QUERIES = {
    "DATABASE_SPACE": DATABASE_SPACE, "DATABASE_FILE_IO": DATABASE_FILE_IO,
}
