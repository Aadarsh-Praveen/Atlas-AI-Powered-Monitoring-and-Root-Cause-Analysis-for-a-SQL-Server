"""Turns the collector's queries into records for the tables in observability/schema.yml.

One Collector object lives as long as the service. Each cycle (every minute)
the service calls run_cycle(); the Collector runs the collections that are
due and returns their records. It doesn't send anything: the senders (B2,
batch 2) do that, and only after they've stored the records safely does the
service call commit() so the watermarks move forward. A crash in between
means some events are sent twice, never that some are lost.

Collections and how often they run (from the schema):
    every 1 min    waits, performance counters, sessions, blocking, Extended Events
    every 5 min    databases + file I/O, job runs, backups, ETL runs,
                   ReportingDB refreshes, restore tests, CommandLog
    every 15 min   top queries (Query Store), vendor check

Three kinds of collection:
    snapshot   what's true now (sessions, databases)
    delta      SQL Server counts since its last restart (waits, file I/O,
               rate counters); we send the difference since the last snapshot.
               After a restart (or on the collector's first cycle) there's no
               previous snapshot, so that cycle sends nothing for them.
    event      rows that happened once (job runs, backups, ETL runs, XE
               events, Query Store intervals), sent once each, tracked with a
               watermark saved on disk.

Every collection runs in its own try/except: one failing query (a timeout, a
missing permission, a database being restored) is counted and reported in the
heartbeat, and the others still run.
"""

import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyodbc

import sql_queries as q
import xevents

# Waits that are always present and say nothing about load: background
# threads sleeping until there's work. Leaving them in would bury the waits
# that matter (locks, I/O, CPU). The usual list used by SQL Server tools.
BENIGN_WAITS = frozenset("""
BROKER_EVENTHANDLER BROKER_RECEIVE_WAITFOR BROKER_TASK_STOP BROKER_TO_FLUSH BROKER_TRANSMITTER
CHECKPOINT_QUEUE CHKPT CLR_AUTO_EVENT CLR_MANUAL_EVENT CLR_SEMAPHORE DBMIRROR_DBM_EVENT
DBMIRROR_EVENTS_QUEUE DBMIRROR_WORKER_QUEUE DBMIRRORING_CMD DIRTY_PAGE_POLL
DISPATCHER_QUEUE_SEMAPHORE EXECSYNC FSAGENT FT_IFTS_SCHEDULER_IDLE_WAIT FT_IFTSHC_MUTEX
HADR_CLUSAPI_CALL HADR_FILESTREAM_IOMGR_IOCOMPLETION HADR_LOGCAPTURE_WAIT
HADR_NOTIFICATION_DEQUEUE HADR_TIMER_TASK HADR_WORK_QUEUE KSOURCE_WAKEUP LAZYWRITER_SLEEP
LOGMGR_QUEUE MEMORY_ALLOCATION_EXT ONDEMAND_TASK_QUEUE PARALLEL_REDO_DRAIN_WORKER
PARALLEL_REDO_LOG_CACHE PARALLEL_REDO_TRAN_LIST PARALLEL_REDO_WORKER_SYNC
PARALLEL_REDO_WORKER_WAIT_WORK PREEMPTIVE_XE_GETTARGETSTATE PWAIT_ALL_COMPONENTS_INITIALIZED
PWAIT_DIRECTLOGCONSUMER_GETNEXT PWAIT_EXTENSIBILITY_CLEANUP_TASK
QDS_PERSIST_TASK_MAIN_LOOP_SLEEP QDS_ASYNC_QUEUE QDS_CLEANUP_STALE_QUERIES_TASK_MAIN_LOOP_SLEEP
QDS_SHUTDOWN_QUEUE REDO_THREAD_PENDING_WORK REQUEST_FOR_DEADLOCK_SEARCH RESOURCE_QUEUE
SERVER_IDLE_CHECK SLEEP_BPOOL_FLUSH SLEEP_DBSTARTUP SLEEP_DCOMSTARTUP SLEEP_MASTERDBREADY
SLEEP_MASTERMDREADY SLEEP_MASTERUPGRADED SLEEP_MSDBSTARTUP SLEEP_SYSTEMTASK SLEEP_TASK
SLEEP_TEMPDBSTARTUP SNI_HTTP_ACCEPT SOS_WORK_DISPATCHER SP_SERVER_DIAGNOSTICS_SLEEP
SQLTRACE_BUFFER_FLUSH SQLTRACE_INCREMENTAL_FLUSH_SLEEP SQLTRACE_WAIT_ENTRIES VDI_CLIENT_OTHER
WAIT_FOR_RESULTS WAITFOR WAITFOR_TASKSHUTDOWN WAIT_XTP_RECOVERY WAIT_XTP_HOST_WAIT
WAIT_XTP_OFFLINE_CKPT_NEW_LOG WAIT_XTP_CKPT_CLOSE XE_DISPATCHER_JOIN XE_DISPATCHER_WAIT
XE_TIMER_EVENT XE_LIVE_TARGET_TVF
""".split())

# Which counter instances to keep. Counters not listed keep every instance.
# The per-database counters keep _Total plus each database (Percent Log Used
# per database is the log-full signal); the lock counters only _Total.
COUNTER_INSTANCES = {
    "Lock Waits/sec": {"_Total"},
    "Number of Deadlocks/sec": {"_Total"},
}
SKIP_COUNTER_INSTANCES = {"mssqlsystemresource"}   # SQL Server's hidden system database
RATE_COUNTER_TYPE = 272696576                      # cumulative; turn into per second

JOB_OUTCOMES = {0: "failed", 1: "succeeded", 2: "retry", 3: "cancelled"}   # 4 = in progress: skipped
BACKUP_TYPES = {"D": "FULL", "I": "DIFF", "L": "LOG"}
FIRST_START_LOOKBACK = timedelta(hours=24)         # schema: conventions.incremental

log = logging.getLogger("collector.collect")


@dataclass
class Settings:
    """What the collector needs to know; filled from the .env in batch 3."""
    xe_file_pattern: str                    # e.g. F:\XE\ShopLite_Diagnostics*.xel
    vendor_url: str | None = None           # read-only shoplite_reader URL; None = skip the vendor check
    vendor_connect_timeout: int = 15
    query_timeout: int = 15                 # seconds per SQL query: monitoring must stay light


@dataclass
class CycleResult:
    """What one cycle produced, for the senders and the heartbeat."""
    records: dict[str, list[dict]] = field(default_factory=dict)   # table name -> rows
    queries_run: int = 0                    # collections run this cycle
    failures: list[str] = field(default_factory=list)   # e.g. ["top_queries", "databases[master]"]
    last_error: str | None = None
    sql_ok: bool = True

    def add(self, table: str, rows: list[dict]) -> None:
        if rows:
            self.records.setdefault(table, []).extend(rows)

    def fail(self, name: str, error: Exception | str) -> None:
        """Record a failure by name, and log it with its full message (the
        heartbeat carries only the last message; the log keeps every one)."""
        message = error if isinstance(error, str) else _message(error)
        self.failures.append(name)
        self.last_error = f"{name}: {message}"[:1000]
        log.warning("collection %s failed: %s", name, message)

    @property
    def queries_failed(self) -> int:
        """Collections with at least one failure. A collection that failed for
        one database (databases[master]) counts once, as "databases", so this
        never exceeds queries_run."""
        return len({name.split("[", 1)[0] for name in self.failures})


class State:
    """Watermarks for the event tables, saved as JSON on disk (F:\\collector).

    pending holds the watermarks this cycle reached; they become current only
    when commit() is called, after the records are safely stored."""

    def __init__(self, path: Path):
        self.path = path
        self.current: dict = json.loads(path.read_text()) if path.exists() else {}
        self.pending: dict = {}

    def get(self, key: str, default=None):
        return self.pending.get(key, self.current.get(key, default))

    def set(self, key: str, value) -> None:
        self.pending[key] = value

    def commit(self) -> None:
        if not self.pending:
            return
        self.current.update(self.pending)
        self.pending = {}
        # Write to a temporary file, then rename: a crash mid-write can't
        # leave a half-written state file.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.current, indent=2, default=str))
        os.replace(tmp, self.path)

    def discard(self) -> None:
        self.pending = {}


class Collector:
    def __init__(self, connect, settings: Settings, state: State, clock=time.monotonic):
        """connect: a function returning a new pyodbc connection as shoplite_monitor."""
        self.connect = connect
        self.settings = settings
        self.state = state
        self.clock = clock
        self.conn = None
        self.computer = None
        self.utc_offset = timedelta(0)
        self.sql_start_time = None
        self.previous: dict[str, tuple[float, dict]] = {}   # delta name -> (monotonic time, values)
        self.last_run: dict[str, float] = {}                 # collection name -> monotonic time

    # ------------------------------------------------------------------
    # The cycle
    # ------------------------------------------------------------------
    def run_cycle(self) -> CycleResult:
        result = CycleResult()
        try:
            self._ensure_connection(result)
        except pyodbc.Error as error:
            result.sql_ok = False
            result.fail("connect", error)
            self._close()
            # The vendor check doesn't need SQL Server: still run it when due.
            self._run_if_due("vendor", 900, result, self.collect_vendor)
            return result

        schedule = [
            ("wait_stats", 60, self.collect_wait_stats),
            ("perf_counters", 60, self.collect_perf_counters),
            ("sessions", 60, self.collect_sessions),
            ("blocking", 60, self.collect_blocking),
            ("xevents", 60, self.collect_xevents),
            ("databases", 300, self.collect_databases),
            ("job_runs", 300, self.collect_job_runs),
            ("backups", 300, self.collect_backups),
            ("etl_runs", 300, self.collect_etl_runs),
            ("refresh_runs", 300, self.collect_refresh_runs),
            ("restore_tests", 300, self.collect_restore_tests),
            ("command_log", 300, self.collect_command_log),
            ("top_queries", 900, self.collect_top_queries),
            ("vendor", 900, self.collect_vendor),
        ]
        for name, interval, collect in schedule:
            self._run_if_due(name, interval, result, collect)
        return result

    def _run_if_due(self, name, interval, result, collect) -> None:
        now = self.clock()
        if name in self.last_run and now - self.last_run[name] < interval - 5:   # 5 s slack for timing jitter
            return
        self.last_run[name] = now
        result.queries_run += 1
        try:
            collect(result)
        except Exception as error:                 # any failure, even a bug: count it, keep collecting
            result.fail(name, error)
            if isinstance(error, pyodbc.OperationalError):     # connection lost: reconnect next cycle
                self._close()

    def _ensure_connection(self, result: CycleResult) -> None:
        if self.conn is None:
            self.conn = self.connect()
            self.conn.timeout = self.settings.query_timeout
            self.conn.cursor().execute(q.SESSION_SETUP)
        info = self._rows(q.SERVER_INFO)[0]
        self.computer = info.machine_name
        self.utc_offset = timedelta(minutes=info.utc_offset_minutes)
        if self.sql_start_time is not None and info.sqlserver_start_time != self.sql_start_time:
            self.previous.clear()                  # SQL Server restarted: every counter began again at 0
        self.sql_start_time = info.sqlserver_start_time

    def _close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except pyodbc.Error:
                pass
        self.conn = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _rows(self, sql: str, *params, database: str | None = None):
        """Every SQL Server query goes through sql_queries.run_query (USE as a
        separate statement, empty results skipped)."""
        return q.run_query(self.conn.cursor(), sql, *params, database=database)

    def _utc(self, local: datetime | None) -> datetime | None:
        """SQL Server local time (naive) -> aware UTC. Offset 0 on the VM and container."""
        if local is None:
            return None
        if local.tzinfo is not None:               # datetimeoffset values carry their own offset
            return local.astimezone(timezone.utc)
        return (local - self.utc_offset).replace(tzinfo=timezone.utc)

    def _base(self, time_generated: datetime | None = None) -> dict:
        return {"TimeGenerated": time_generated or datetime.now(timezone.utc), "Computer": self.computer}

    def _delta(self, name: str, values: dict) -> tuple[dict, float] | None:
        """Differences since the previous snapshot of this kind, and the seconds between.
        None on the first snapshot (nothing to compare with). A value that went DOWN
        means its counter was reset: it's skipped rather than sent as negative."""
        now = self.clock()
        previous = self.previous.get(name)
        self.previous[name] = (now, values)
        if previous is None:
            return None
        then, old = previous
        seconds = now - then
        changes = {}
        for key, value in values.items():
            if key in old and isinstance(value, tuple):
                diff = tuple(a - b for a, b in zip(value, old[key]))
                if all(d >= 0 for d in diff):
                    changes[key] = diff
            elif key in old and value >= old[key]:
                changes[key] = value - old[key]
        return changes, seconds

    def _watermark(self, key: str, default):
        return self.state.get(key, default)

    def _since_first_start_local(self) -> datetime:
        """'24 hours ago' in SQL Server's local time, for first-start lookbacks."""
        return (datetime.now(timezone.utc) - FIRST_START_LOOKBACK + self.utc_offset).replace(tzinfo=None)

    # ------------------------------------------------------------------
    # Every minute
    # ------------------------------------------------------------------
    def collect_wait_stats(self, result: CycleResult) -> None:
        values = {r.wait_type: (r.wait_time_ms, r.signal_wait_time_ms, r.waiting_tasks_count)
                  for r in self._rows(q.WAIT_STATS) if r.wait_type not in BENIGN_WAITS}
        delta = self._delta("wait_stats", values)
        if delta is None:
            return
        changes, seconds = delta
        result.add("SqlWaitStats_CL", [
            {**self._base(), "WaitType": wait, "WaitTimeMs": d[0], "SignalWaitTimeMs": d[1],
             "WaitingTasks": d[2], "IntervalSeconds": round(seconds, 1)}
            for wait, d in changes.items() if d[0] > 0])

    def collect_perf_counters(self, result: CycleResult) -> None:
        rows = []
        cumulative = {}
        for r in self._rows(q.PERF_COUNTERS):
            if r.instance_name in SKIP_COUNTER_INSTANCES:
                continue
            allowed = COUNTER_INSTANCES.get(r.counter_name)
            if allowed is not None and r.instance_name not in allowed:
                continue
            # "SQLServer:Buffer Manager" (or "MSSQL$NAME:Buffer Manager") -> "Buffer Manager"
            obj = r.object_name.split(":", 1)[-1]
            key = (obj, r.counter_name, r.instance_name)
            if r.cntr_type == RATE_COUNTER_TYPE:
                cumulative[key] = r.cntr_value
            else:
                rows.append(self._counter(obj, r.counter_name, r.instance_name, float(r.cntr_value)))
        delta = self._delta("perf_counters", cumulative)
        if delta is not None:
            changes, seconds = delta
            for (obj, counter, instance), diff in changes.items():
                rows.append(self._counter(obj, counter, instance, round(diff / seconds, 3) if seconds else 0.0))
        result.add("SqlPerfCounters_CL", rows)

    def _counter(self, obj, counter, instance, value) -> dict:
        return {**self._base(), "ObjectName": obj, "CounterName": counter,
                "InstanceName": instance, "Value": value}

    def collect_sessions(self, result: CycleResult) -> None:
        result.add("SqlSessions_CL", [
            {**self._base(), "DatabaseName": r.database_name, "ProgramName": r.program_name,
             "LoginName": r.login_name, "HostName": r.host_name, "Sessions": r.sessions,
             "RunningRequests": r.running_requests, "BlockedRequests": r.blocked_requests,
             "OpenTransactionSessions": r.open_transaction_sessions,
             "LongestRequestSeconds": _not_negative(r.longest_request_seconds)}
            for r in self._rows(q.SESSIONS)])

    def collect_blocking(self, result: CycleResult) -> None:
        blocked = {r.session_id: r for r in self._rows(q.BLOCKED_REQUESTS)}
        if not blocked:
            return
        # Walk each chain upward: a head blocker is a session that blocks
        # others but isn't itself blocked. (The seen-set guards against a
        # cycle, which SQL Server resolves as a deadlock within seconds.)
        def head_of(session_id: int) -> int:
            seen = set()
            while session_id in blocked and session_id not in seen:
                seen.add(session_id)
                session_id = blocked[session_id].blocking_session_id
            return session_id

        heads = {sid: head_of(sid) for sid in blocked}
        involved = set(blocked) | set(heads.values())
        details = {r.session_id: r for r in self._rows(q.SESSION_DETAILS, ",".join(map(str, sorted(involved))))}
        now = datetime.now(timezone.utc)
        rows = []
        for sid in sorted(involved):
            d = details.get(sid)
            if d is None:                          # ended between the two queries
                continue
            request = blocked.get(sid)
            begin = self._utc(d.oldest_transaction_begin)
            rows.append({
                **self._base(now),
                "Role": "blocked" if request else "head_blocker",
                "SessionId": sid,
                "BlockedBy": request.blocking_session_id if request else None,
                "HeadBlocker": heads.get(sid, sid),
                "WaitType": request.wait_type if request else None,
                "WaitSeconds": round(request.wait_time_ms / 1000.0, 1) if request else None,
                "WaitResource": request.wait_resource if request else None,
                "DatabaseName": d.database_name, "ProgramName": d.program_name,
                "LoginName": d.login_name, "HostName": d.host_name, "SessionStatus": d.status,
                "OpenTransactions": d.open_transaction_count,
                "TransactionSeconds": _not_negative(round((now - begin).total_seconds(), 1)) if begin else None,
                "StatementText": d.statement_text,
            })
        result.add("SqlBlocking_CL", rows)

    def collect_xevents(self, result: CycleResult) -> None:
        position = self.state.get("xe_position")       # {"file": ..., "offset": ...} or None
        since = None if position else datetime.now(timezone.utc) - FIRST_START_LOOKBACK
        rows, new_position = xevents.read_new_events(
            self.conn.cursor(), self.settings.xe_file_pattern, position, since, self.computer)
        result.add("SqlXEvents_CL", rows)
        if new_position:
            self.state.set("xe_position", new_position)

    # ------------------------------------------------------------------
    # Every 5 minutes
    # ------------------------------------------------------------------
    def collect_databases(self, result: CycleResult) -> None:
        """Databases (one row each) and their files' I/O, in one pass over the databases."""
        now = datetime.now(timezone.utc)
        db_rows, io_values = [], {}
        for d in self._rows(q.DATABASES):
            row = {**self._base(now), "DatabaseName": d.name, "State": d.state_desc,
                   "RecoveryModel": d.recovery_model_desc, "LogReuseWait": d.log_reuse_wait_desc,
                   "DataSizeMB": None, "DataUsedMB": None, "LogSizeMB": None, "LogUsedPercent": None,
                   "LastFullBackupAt": self._utc(d.last_full), "LastDiffBackupAt": self._utc(d.last_diff),
                   "LastLogBackupAt": self._utc(d.last_log)}
            if d.state_desc == "ONLINE" and d.has_access == 1:
                # One database failing (e.g. a missing permission) costs only
                # its own numbers: its row is still sent, the others unaffected.
                try:
                    space = self._rows(q.DATABASE_SPACE, database=d.name)[0]
                    row.update({"DataSizeMB": _round(space.data_size_mb), "DataUsedMB": _round(space.data_used_mb),
                                "LogSizeMB": _round(space.log_size_mb),
                                "LogUsedPercent": _round(space.log_used_percent)})
                    for f in self._rows(q.DATABASE_FILE_IO, database=d.name):
                        io_values[(d.name, f.file_name, f.type_desc)] = (
                            f.num_of_reads, f.num_of_writes, f.num_of_bytes_read, f.num_of_bytes_written,
                            f.io_stall_read_ms, f.io_stall_write_ms)
                except pyodbc.Error as error:
                    result.fail(f"databases[{d.name}]", error)
            db_rows.append(row)
        result.add("SqlDatabases_CL", db_rows)

        delta = self._delta("file_io", io_values)
        if delta is not None:
            changes, seconds = delta
            result.add("SqlFileIo_CL", [
                {**self._base(now), "DatabaseName": db, "FileLogicalName": name, "FileType": kind,
                 "Reads": c[0], "Writes": c[1], "ReadBytes": c[2], "WriteBytes": c[3],
                 "ReadStallMs": c[4], "WriteStallMs": c[5], "IntervalSeconds": round(seconds, 1)}
                for (db, name, kind), c in changes.items()])

    def collect_job_runs(self, result: CycleResult) -> None:
        last_id = self._watermark("job_runs", None)
        rows = self._rows(q.JOB_RUNS, last_id or 0,
                          datetime(1900, 1, 1) if last_id else self._since_first_start_local())
        out = []
        for r in rows:
            last_id = r.instance_id
            outcome = JOB_OUTCOMES.get(r.run_status)
            if outcome is None:
                continue
            seconds = _hhmmss_seconds(r.run_duration)
            started = self._utc(r.started_at)
            failed = outcome == "failed"
            out.append({**self._base(started + timedelta(seconds=seconds)),
                        "JobName": r.job_name, "Category": r.category, "Outcome": outcome,
                        "StartedAt": started, "DurationSeconds": seconds, "Message": r.message,
                        "FailedStepName": r.failed_step_name if failed else None,
                        "FailedStepMessage": r.failed_step_message if failed else None,
                        "HistoryId": r.instance_id})
        result.add("SqlAgentJobRuns_CL", out)
        if rows:
            self.state.set("job_runs", last_id)

    def collect_backups(self, result: CycleResult) -> None:
        last_id = self._watermark("backups", None)
        rows = self._rows(q.BACKUPS, last_id or 0,
                          datetime(1900, 1, 1) if last_id else self._since_first_start_local())
        result.add("SqlBackups_CL", [
            {**self._base(self._utc(r.backup_finish_date)), "DatabaseName": r.database_name,
             "BackupType": BACKUP_TYPES.get(r.type, r.type), "StartedAt": self._utc(r.backup_start_date),
             "SizeMB": _round(r.size_mb), "CompressedMB": _round(r.compressed_mb),
             "IsCopyOnly": bool(r.is_copy_only), "Device": r.physical_device_name,
             "BackupSetId": r.backup_set_id}
            for r in rows])
        if rows:
            self.state.set("backups", rows[-1].backup_set_id)

    def _finished_rows(self, key: str, sql: str, finish_column: str = "finished_at"):
        """Rows from an ops table that finished after the watermark (SQL Server local time)."""
        last = self._watermark(key, None)
        since = datetime.fromisoformat(last) if last else self._since_first_start_local()
        rows = self._rows(sql, since)
        if rows:
            self.state.set(key, getattr(rows[-1], finish_column).isoformat())
        return rows

    def collect_etl_runs(self, result: CycleResult) -> None:
        result.add("OpsEtlRuns_CL", [
            {**self._base(self._utc(r.finished_at)), "RunId": r.run_id, "Feed": r.feed,
             "ContractVersion": r.contract_version, "StartedAt": self._utc(r.started_at),
             "Status": r.status, "DurationSeconds": _seconds_between(r.started_at, r.finished_at),
             "RowsRead": r.rows_read, "RowsSkipped": r.rows_skipped, "RowsInserted": r.rows_inserted,
             "RowsUpdated": r.rows_updated, "RowsDiscontinued": r.rows_discontinued,
             "RowsUnchanged": r.rows_unchanged, "VendorNewestPriceAt": self._utc(r.vendor_newest_price_at),
             "ErrorStage": r.error_stage, "ErrorMessage": r.error_message}
            for r in self._finished_rows("etl_runs", q.ETL_RUNS)])

    def collect_refresh_runs(self, result: CycleResult) -> None:
        result.add("OpsRefreshRuns_CL", [
            {**self._base(self._utc(r.finished_at)), "RunId": r.run_id, "Mode": r.mode,
             "FromDate": _date_utc(r.from_date), "CutoffAt": self._utc(r.cutoff_at),
             "StartedAt": self._utc(r.started_at), "Status": r.status,
             "DurationSeconds": _seconds_between(r.started_at, r.finished_at),
             "SaleLines": r.sale_lines, "DaysRebuilt": r.days_rebuilt, "RowsWritten": r.rows_written,
             "ErrorMessage": r.error_message}
            for r in self._finished_rows("refresh_runs", q.REFRESH_RUNS)])

    def collect_restore_tests(self, result: CycleResult) -> None:
        result.add("DbaRestoreTests_CL", [
            {**self._base(self._utc(r.finished_at)), "RunId": r.run_id, "StartedAt": self._utc(r.started_at),
             "Status": r.status, "FullBackupFinishedAt": self._utc(r.full_backup_finished_at),
             "LogBackupsRestored": r.log_backups_restored, "RestoredTo": self._utc(r.restored_to),
             "RestoreSeconds": r.restore_seconds, "CheckdbSeconds": r.checkdb_seconds,
             "ErrorMessage": r.error_message}
            for r in self._finished_rows("restore_tests", q.RESTORE_TESTS)])

    def collect_command_log(self, result: CycleResult) -> None:
        result.add("DbaCommandLog_CL", [
            {**self._base(self._utc(r.EndTime)), "CommandLogId": r.ID, "DatabaseName": r.DatabaseName,
             "CommandType": r.CommandType, "ObjectName": r.object_name, "IndexName": r.IndexName,
             "StartedAt": self._utc(r.StartTime), "DurationSeconds": _seconds_between(r.StartTime, r.EndTime),
             "ErrorNumber": r.ErrorNumber, "ErrorMessage": r.error_message, "Command": r.command}
            for r in self._finished_rows("command_log", q.COMMAND_LOG, finish_column="EndTime")])

    # ------------------------------------------------------------------
    # Every 15 minutes
    # ------------------------------------------------------------------
    def collect_top_queries(self, result: CycleResult) -> None:
        for (database,) in self._rows(q.QUERY_STORE_DATABASES):
            key = f"query_store:{database}"
            last_end = self._watermark(key, None)      # naive UTC, ISO text
            since = (datetime.fromisoformat(last_end) if last_end
                     else (datetime.now(timezone.utc) - FIRST_START_LOOKBACK).replace(tzinfo=None))
            intervals = self._rows(q.QUERY_STORE_INTERVALS, since, database=database)
            for interval in intervals:
                end = interval.end_utc.replace(tzinfo=timezone.utc)
                interval_id = interval.runtime_stats_interval_id
                result.add("SqlTopQueries_CL", [
                    {**self._base(end), "EventId": f"{database}:{interval_id}:{r.plan_id}",
                     "DatabaseName": database,
                     "WindowStart": interval.start_utc.replace(tzinfo=timezone.utc),
                     "QueryId": r.query_id, "PlanId": r.plan_id, "QueryHash": r.query_hash,
                     "PlanHash": r.plan_hash, "IsForcedPlan": bool(r.is_forced_plan),
                     "Executions": r.executions,
                     "TotalDurationMs": _round(r.total_duration_ms), "TotalCpuMs": _round(r.total_cpu_ms),
                     "TotalLogicalReads": _round(r.total_logical_reads),
                     "MaxDurationMs": _round(r.max_duration_ms), "PlansForQuery": r.plans_for_query,
                     "QueryText": r.query_text}
                    for r in self._rows(q.QUERY_STORE_TOP, interval_id, database=database)])
            if intervals:
                self.state.set(key, intervals[-1].end_utc.isoformat())

    def collect_vendor(self, result: CycleResult) -> None:
        """A light check of the vendor's Postgres as the read-only login. Not a SQL
        Server query: a failure here is a finding (Reachable = false), not an error."""
        if not self.settings.vendor_url:
            return
        import psycopg                                 # only needed when the check is configured
        row = {**self._base(), "Reachable": False, "ConnectMs": None, "NewestPriceAt": None,
               "FreshnessHours": None, "CatalogItems": None, "Error": None}
        if row["Computer"] is None:                    # SQL Server unreachable this cycle
            row["Computer"] = socket.gethostname()
        started = time.monotonic()
        try:
            with psycopg.connect(self.settings.vendor_url, connect_timeout=self.settings.vendor_connect_timeout,
                                 options="-c statement_timeout=15000", application_name="ShopLite-Collector") as vendor:
                row["ConnectMs"] = round((time.monotonic() - started) * 1000, 1)
                newest, vendor_now, items = vendor.execute(q.VENDOR_FRESHNESS).fetchone()
            row.update({"Reachable": True, "CatalogItems": items,
                        "NewestPriceAt": newest.astimezone(timezone.utc) if newest else None,
                        "FreshnessHours": round((vendor_now - newest).total_seconds() / 3600, 2) if newest else None})
        except psycopg.Error as error:
            row["Error"] = str(error).strip()[:1000]
        result.add("VendorFeed_CL", [row])


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _message(error: Exception) -> str:
    if isinstance(error, pyodbc.Error) and error.args:
        return str(error.args[-1])
    return str(error) or type(error).__name__


def _not_negative(value):
    """Elapsed times can come out a hair below zero (-0.001 s): SQL Server
    stamps a request's start and reads its clock separately, and the two can
    differ by a millisecond. Nothing can have run for negative time: clamp at 0."""
    return None if value is None else max(0.0, float(value))


def _round(value, digits: int = 2):
    return None if value is None else round(float(value), digits)


def _hhmmss_seconds(value: int) -> int:
    """msdb stores durations as HHMMSS integers: 13542 = 1 h 35 min 42 s."""
    hours, rest = divmod(int(value), 10000)
    minutes, seconds = divmod(rest, 100)
    return hours * 3600 + minutes * 60 + seconds


def _seconds_between(start: datetime | None, end: datetime | None):
    return None if start is None or end is None else round((end - start).total_seconds(), 1)


def _date_utc(value):
    """A DATE column (midnight UTC, as the schema says) -> aware datetime."""
    return None if value is None else datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
