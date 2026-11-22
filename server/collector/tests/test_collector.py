"""Tests for the monitoring collector, against your LOCAL SQL Server only.

Run from the repo root, with the collector's virtual environment:

    server/collector/.venv/bin/python -m pytest server/collector/tests -v

Needs (README.md, "Local"): Phase 4 set up locally (SalesDB, ReportingDB, DBA
with Ola, the XE session), sql/001_monitor_login.sql applied, and the root
.env (SHOPLITE_MONITOR_PASSWORD; also the local SQL admin, used ONLY to set up
the blocking scenario). No server/collector/.env needed: that's the VM's file. Anything not on localhost
stops the run. A database that isn't reachable makes its tests SKIP.

What the groups prove:
  permissions   every query the collector runs works as shoplite_monitor
                (one test per query, so a missing permission names the query),
                the login holds VIEW SERVER PERFORMANCE STATE and not the
                broader VIEW SERVER STATE, and it can't touch business data.
                If a query fails ONLY because of the narrower permission, the
                login goes back to VIEW SERVER STATE and the login script
                documents which query and why (decided with the owner).
  cycles        two real collector cycles: no collection fails, and every
                record matches observability/schema.yml exactly.
  blocking      a real blocking chain is reported as head blocker + blocked.
  offline       the XE parser and its IDs, the buffer and retries, and the
                ADX script matching the schema (no database needed).
"""

import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyodbc
import pytest
import yaml

COLLECTOR_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = COLLECTOR_DIR.parent.parent
sys.path.insert(0, str(COLLECTOR_DIR))

import main  # noqa: E402
import sql_queries as q  # noqa: E402
import xevents  # noqa: E402
from buffer import Buffer  # noqa: E402
from collect import Collector, State  # noqa: E402

# Layered settings, exactly as the collector reads them: server/collector/.env
# if present, then the root .env (which also has SA_PASSWORD / SQL_ADMIN_USER,
# used for the blocking setup only).
sys.path.insert(0, str(REPO_ROOT / "tools"))
from envconfig import load_layers  # noqa: E402
load_layers(COLLECTOR_DIR)

SCHEMA = yaml.safe_load((REPO_ROOT / "observability" / "schema.yml").read_text(encoding="utf-8"))
ACCESSIBLE_DATABASES = ["master", "tempdb", "msdb", "SalesDB", "ReportingDB", "DBA"]
QUERY_STORE_DATABASES = ["SalesDB", "ReportingDB"]
OLD = datetime(2000, 1, 1)                  # "since forever" for watermark parameters


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture(scope="session")
def config():
    try:
        loaded = main.load_config()
    except main.ConfigError as error:
        pytest.skip(f"collector settings (root .env, or server/collector/.env if present): {error}")
    if loaded.sql_host not in ("localhost", "127.0.0.1"):
        pytest.exit(f"SQL_HOST is '{loaded.sql_host}'. These tests run against local SQL Server only.", returncode=2)
    return loaded


@pytest.fixture(scope="session")
def connect_monitor(config):
    connect = main.sql_connector(config)
    try:
        connect().close()
    except pyodbc.Error as error:
        pytest.skip(f"SQL Server not reachable as shoplite_monitor: {error.args[-1]}")
    return connect


@pytest.fixture
def monitor(connect_monitor, config):
    conn = connect_monitor()
    conn.timeout = config.collect.query_timeout
    conn.cursor().execute(q.SESSION_SETUP)
    yield conn
    conn.close()


def admin_connection(config, app_name: str):
    import os
    password = os.getenv("SA_PASSWORD")
    if not password:
        pytest.skip("root .env has no SA_PASSWORD (needed only to set up the blocking test)")
    user = (os.getenv("SQL_ADMIN_USER") or "sa").replace("}", "}}")
    password = password.replace("}", "}}")            # braces make ODBC take the value literally
    return pyodbc.connect(
        f"DRIVER={{{main.ODBC_DRIVER}}};SERVER={config.sql_host},{config.sql_port};DATABASE=tempdb;"
        f"UID={{{user}}};PWD={{{password}}};Encrypt=yes;TrustServerCertificate=yes;"
        f"APP={app_name};", timeout=10, autocommit=False)


# -----------------------------------------------------------------------------
# Permissions: every collector query, as shoplite_monitor
# -----------------------------------------------------------------------------
def query_cases():
    """(test id, sql, params, database). Parameters are harmless values that
    make each query run for real (old watermarks, our own session ID)."""
    cases = [(name, sql, (), None) for name, sql in q.SERVER_QUERIES.items()]
    cases += [(f"{name}[{db}]", sql, (), db) for name, sql in q.PER_DATABASE_QUERIES.items()
              for db in ACCESSIBLE_DATABASES]
    cases += [
        ("SESSION_DETAILS", q.SESSION_DETAILS, ("@@SPID",), None),
        ("XE_EVENTS", q.XE_EVENTS, ("XE_PATTERN", None, None), None),
        ("JOB_RUNS", q.JOB_RUNS, (0, OLD), None),
        ("BACKUPS", q.BACKUPS, (0, OLD), None),
        ("ETL_RUNS", q.ETL_RUNS, (OLD,), None),
        ("REFRESH_RUNS", q.REFRESH_RUNS, (OLD,), None),
        ("RESTORE_TESTS", q.RESTORE_TESTS, (OLD,), None),
        ("COMMAND_LOG", q.COMMAND_LOG, (OLD,), None),
    ]
    cases += [(f"QUERY_STORE_INTERVALS[{db}]", q.QUERY_STORE_INTERVALS, (OLD,), db) for db in QUERY_STORE_DATABASES]
    cases += [(f"QUERY_STORE_TOP[{db}]", q.QUERY_STORE_TOP, ("LATEST_INTERVAL",), db) for db in QUERY_STORE_DATABASES]
    return cases


@pytest.mark.parametrize("name,sql,params,database", query_cases(), ids=[c[0] for c in query_cases()])
def test_query_runs_as_monitor(monitor, config, name, sql, params, database):
    # Exactly as the collector runs them: through run_query (USE separately).
    cursor = monitor.cursor()
    resolved = []
    for p in params:
        if p == "@@SPID":
            p = str(q.run_query(cursor, "SELECT @@SPID")[0][0])
        elif p == "XE_PATTERN":
            p = config.collect.xe_file_pattern
        elif p == "LATEST_INTERVAL":
            row = q.run_query(cursor, "SELECT MAX(runtime_stats_interval_id) FROM sys.query_store_runtime_stats_interval",
                              database=database)[0]
            p = row[0] or 0
        resolved.append(p)
    try:
        q.run_query(cursor, sql, *resolved, database=database)
    except pyodbc.Error as error:
        message = str(error.args[-1])
        hint = (" -> a PERMISSION problem: check whether VIEW SERVER STATE (instead of "
                "VIEW SERVER PERFORMANCE STATE) or a grant in 001_monitor_login.sql is missing"
                if "permission" in message.lower() or "denied" in message.lower() else "")
        pytest.fail(f"{name} failed as shoplite_monitor: {message}{hint}")


def test_monitor_holds_the_narrow_server_permission(monitor):
    permissions = {r[0] for r in monitor.cursor().execute(
        "SELECT permission_name FROM sys.fn_my_permissions(NULL, 'SERVER')").fetchall()}
    assert "VIEW SERVER PERFORMANCE STATE" in permissions
    assert "VIEW SERVER STATE" not in permissions, (
        "shoplite_monitor has VIEW SERVER STATE: re-run 001_monitor_login.sql (it revokes it), "
        "unless a query was proven to need it and the script documents why")


@pytest.mark.parametrize("statement", [
    "SELECT TOP 1 * FROM SalesDB.dbo.orders",
    "SELECT TOP 1 * FROM SalesDB.vendor.supplier_prices",
    "SELECT TOP 1 * FROM ReportingDB.rpt.daily_sales",
    "UPDATE SalesDB.ops.etl_runs SET status = status WHERE 1 = 0",
    "DELETE FROM DBA.dbo.CommandLog WHERE 1 = 0",
])
def test_monitor_cannot_touch_business_data(monitor, statement):
    with pytest.raises(pyodbc.Error, match="permission was denied"):
        monitor.cursor().execute(statement)


# -----------------------------------------------------------------------------
# Two real cycles, and every record against the schema
# -----------------------------------------------------------------------------
PYTHON_TYPES = {"string": (str,), "int": (int,), "long": (int,), "real": (float, int), "datetime": (datetime,)}


def check_record(table: str, record: dict) -> list[str]:
    """Problems with one record: missing or extra columns, wrong value types."""
    columns = {c["name"]: c["type"] for c in SCHEMA["tables"][table]["columns"]}
    problems = []
    if set(record) != set(columns):
        problems.append(f"{table}: missing {sorted(set(columns) - set(record))}, extra {sorted(set(record) - set(columns))}")
    for name, kind in columns.items():
        value = record.get(name)
        if value is None:
            continue
        if kind == "bool":
            ok = isinstance(value, bool)
        elif kind in ("int", "long"):
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, PYTHON_TYPES[kind])
        if kind == "datetime" and ok and value.tzinfo is None:
            ok = False                              # every datetime must be UTC-aware
        if not ok:
            problems.append(f"{table}.{name}: expected {kind}, got {type(value).__name__} ({value!r:.60})")
    return problems


@pytest.fixture
def two_cycles(connect_monitor, config, tmp_path):
    """Run a cycle, advance the collector's clock 5 minutes, run another, so the
    delta tables have something to compare. Returns both results.

    The clock decides which collections are due (collect.py, _run_if_due):
    the first cycle runs all of them; the second runs only those whose
    interval has passed. 301 seconds is past both the 1-minute and the
    5-minute intervals (each allows 5 s of slack), so the second cycle runs
    waits and counters AND databases + file I/O, and every delta table gets
    rows. The 15-minute collections (Query Store, vendor) ran in the first
    cycle and aren't deltas, so they're covered without a third cycle.

    Only the collector's clock jumps; real time moves 2 seconds, enough for
    SQL Server's counters to change. Deltas divided by the jumped clock give
    small per-second rates, which is fine: these tests check that rows exist
    and match the schema, not their values."""
    now = [time.monotonic()]
    collector = Collector(connect_monitor, config.collect, State(tmp_path / "state.json"), clock=lambda: now[0])
    first = collector.run_cycle()
    time.sleep(2)                                   # let real counters move a little
    now[0] += 301
    second = collector.run_cycle()
    return collector, first, second


def test_cycles_run_without_failures(two_cycles):
    _, first, second = two_cycles
    for result in (first, second):
        assert result.sql_ok
        assert not result.failures, f"collections failed: {result.failures} (last: {result.last_error})"


def test_every_record_matches_the_schema(two_cycles):
    _, first, second = two_cycles
    problems = []
    for result in (first, second):
        for table, records in result.records.items():
            assert table in SCHEMA["tables"], f"{table} isn't in schema.yml"
            for record in records:
                problems += check_record(table, record)
    assert not problems, "\n".join(problems[:20])


def test_expected_tables_are_produced(two_cycles):
    _, first, second = two_cycles
    produced = set(first.records) | set(second.records)
    # Always present on a running server (the event tables depend on history).
    for table in ("SqlPerfCounters_CL", "SqlSessions_CL", "SqlDatabases_CL", "SqlWaitStats_CL", "SqlFileIo_CL"):
        assert table in produced, f"no {table} rows"


def test_event_tables_have_their_unique_id(two_cycles):
    _, first, second = two_cycles
    for result in (first, second):
        for table, records in result.records.items():
            unique_id = SCHEMA["tables"][table].get("unique_id")
            if unique_id:
                ids = [r[unique_id] for r in records]
                assert all(i is not None for i in ids), f"{table}: null {unique_id}"
                assert len(ids) == len(set(ids)), f"{table}: duplicate {unique_id} within one cycle"


# -----------------------------------------------------------------------------
# Blocking: a real chain
# -----------------------------------------------------------------------------
def test_blocking_chain_is_reported(connect_monitor, config, tmp_path):
    holder = admin_connection(config, "collector-test-holder")
    waiter = admin_connection(config, "collector-test-waiter")
    try:
        setup = holder.cursor()
        setup.execute("IF OBJECT_ID('tempdb..##collector_block') IS NULL CREATE TABLE ##collector_block (id INT PRIMARY KEY, v INT);")
        setup.execute("IF NOT EXISTS (SELECT 1 FROM ##collector_block) INSERT ##collector_block VALUES (1, 0);")
        holder.commit()
        setup.execute("UPDATE ##collector_block SET v = v + 1 WHERE id = 1;")    # lock held: transaction left open

        def wait_for_lock():
            try:
                c = waiter.cursor()
                c.execute("SET LOCK_TIMEOUT 20000; UPDATE ##collector_block SET v = v + 1 WHERE id = 1;")
            except pyodbc.Error:
                pass
        thread = threading.Thread(target=wait_for_lock, daemon=True)
        thread.start()
        time.sleep(3)                                # let it start waiting

        collector = Collector(connect_monitor, config.collect, State(tmp_path / "state.json"))
        collector.run_cycle()                        # connects, reads server info
        from collect import CycleResult
        result = CycleResult()
        collector.collect_blocking(result)
        rows = result.records.get("SqlBlocking_CL", [])
        heads = [r for r in rows if r["Role"] == "head_blocker" and r["ProgramName"] == "collector-test-holder"]
        blocked = [r for r in rows if r["Role"] == "blocked" and r["ProgramName"] == "collector-test-waiter"]
        assert heads, f"head blocker not reported: {rows}"
        assert blocked and blocked[0]["WaitType"].startswith("LCK_M_"), f"blocked session not reported: {rows}"
        assert blocked[0]["HeadBlocker"] == heads[0]["SessionId"]
        assert heads[0]["SessionStatus"] == "sleeping" and heads[0]["OpenTransactions"] >= 1
        assert heads[0]["TransactionSeconds"] and heads[0]["TransactionSeconds"] > 0
        assert "UPDATE ##collector_block" in (heads[0]["StatementText"] or "")
        for r in rows:
            assert not check_record("SqlBlocking_CL", r)
    finally:
        holder.rollback()
        holder.close()
        waiter.close()


# -----------------------------------------------------------------------------
# Offline: no database needed
# -----------------------------------------------------------------------------
class FakeCursor:
    description = [("column",)]                  # "has a result set", as run_query checks

    def __init__(self, rows):
        self.rows = rows

    def execute(self, *args):
        return self

    def nextset(self):
        return False

    def fetchall(self):
        return self.rows


class XeRow:
    def __init__(self, file_name, offset, minute, name="error_reported"):
        self.file_name, self.file_offset, self.object_name = file_name, offset, name
        self.timestamp_utc = datetime(2026, 11, 22, 15, minute)
        self.event_data = f'<event name="{name}"><data name="error_number"><value>50000</value></data></event>'


def test_xe_event_ids_are_unique_and_stable():
    rows = [XeRow(r"F:\XE\S_0_1.xel", 100, 1), XeRow(r"F:\XE\S_0_1.xel", 100, 2), XeRow(r"F:\XE\S_0_1.xel", 900, 3)]
    first, position = xevents.read_new_events(FakeCursor(rows), "p", None, None, "x")
    again, _ = xevents.read_new_events(FakeCursor(rows), "p", None, None, "x")
    ids = [r["EventId"] for r in first]
    assert ids == ["S_0_1.xel:100:0", "S_0_1.xel:100:1", "S_0_1.xel:900:0"]
    assert ids == [r["EventId"] for r in again]                    # same events -> same IDs
    assert position["offset"] == 900
    for r in first:
        assert not check_record("SqlXEvents_CL", r)


def test_xe_first_start_keeps_only_recent_events():
    rows = [XeRow("S.xel", 1, 1), XeRow("S.xel", 2, 30)]
    since = datetime(2026, 11, 22, 15, 10, tzinfo=timezone.utc)
    kept, _ = xevents.read_new_events(FakeCursor(rows), "p", None, since, "x")
    assert [r["TimeGenerated"].minute for r in kept] == [30]


class FlakySender:
    name = "flaky"

    def __init__(self, failures):
        self.failures, self.sent = failures, []

    def send(self, table, records):
        if self.failures:
            self.failures -= 1
            raise ConnectionError("destination down")
        self.sent.append((table, len(records)))


def test_buffer_keeps_records_until_the_destination_accepts_them(tmp_path):
    destination = main.Destination(FlakySender(failures=1), Buffer(tmp_path, "flaky", timedelta(hours=1), 10_000_000))
    destination.backoff.clock = lambda: 0.0
    destination.buffer.store("OpsEtlRuns_CL", [{"RunId": 1}, {"RunId": 2}])
    destination.flush()                              # fails: file stays, back-off starts
    assert destination.ok is False and destination.buffer.record_count() == 2
    destination.flush()                              # still backing off: nothing tried
    assert destination.sender.sent == []
    destination.backoff.clock = lambda: 10_000.0     # time passes
    destination.flush()
    assert destination.ok is True and destination.sender.sent == [("OpsEtlRuns_CL", 2)]
    assert destination.buffer.record_count() == 0


def test_buffer_drops_files_past_their_age(tmp_path):
    buffer = Buffer(tmp_path, "aged", timedelta(hours=47), 10_000_000)
    old = tmp_path / "aged" / "20200101T000000000000_SqlBackups_CL_3.jsonl"
    old.write_text('{"BackupSetId": 1}\n')
    buffer.enforce_limits()
    assert not old.exists()


def test_adx_script_matches_the_schema():
    script = (REPO_ROOT / "observability" / "adx" / "001_shoplite_database.kql").read_text(encoding="utf-8")
    for table, spec in SCHEMA["tables"].items():
        match = re.search(rf"^\.create-merge table {table} \((.*)\)$", script, re.MULTILINE)
        assert match, f"{table} missing from the ADX script"
        adx_columns = [part.split(":")[0].strip() for part in match.group(1).split(",")]
        assert adx_columns == [c["name"] for c in spec["columns"]], f"{table}: ADX columns differ from schema.yml"
