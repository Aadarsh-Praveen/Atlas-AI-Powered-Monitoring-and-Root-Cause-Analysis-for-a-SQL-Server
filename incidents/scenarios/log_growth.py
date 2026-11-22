"""Log growth: an open transaction keeps the log from being reused.

Story: someone testing the vendor ETL by hand in SSMS reloads the staging
table again and again inside one explicit transaction, and leaves it open.
In FULL recovery the log can only be reused once log backups have copied it
AND no open transaction still needs it. This one needs all of it, so log
backups run but free nothing (log_reuse_wait = ACTIVE_TRANSACTION), the log
fills up, and it grows.

What it does: one session as the SQL admin ("SSMS") opens a transaction and,
every 15 seconds, empties vendor.supplier_prices_staging and refills it from
vendor.supplier_prices (about 10,000 rows): lots of log, no real data at
risk (staging is scratch space, emptied by every ETL run, and the final
ROLLBACK restores exactly what was there).

Safety, because a FULL log stops every write in SalesDB:
  - writing pauses while the log is more than 85% used, so it can't fill;
  - writing stops for good once the log file has grown 1 GB (the data disk is
    64 GB), keeping the transaction open but adding nothing;
  - in production it refuses to overlap the 14:00 vendor ETL, which needs the
    staging table (and would block behind this transaction).
Revert: ROLLBACK (can take a minute), then the next log backup frees the log.

Session-only: if the injector dies, SQL Server rolls the transaction back.
"""

from datetime import datetime, timezone

from framework import Context, Incident, Refused

PROGRAM = "Microsoft SQL Server Management Studio - Query"
PAUSE_ABOVE_PERCENT = 85
MAX_GROWTH_MB = 1024
CYCLE_SECONDS = 15

LOG_STATE = """
SELECT total_log_size_in_bytes / 1048576.0, used_log_space_in_percent FROM sys.dm_db_log_space_usage;
"""
RELOAD = """
DELETE FROM vendor.supplier_prices_staging;
INSERT INTO vendor.supplier_prices_staging
    (supplier_name, supplier_sku, shoplite_sku, unit_cost, supplier_stock, is_discontinued)
SELECT sp.supplier_name, sp.supplier_sku, p.sku, sp.unit_cost, sp.supplier_stock, sp.is_discontinued
FROM vendor.supplier_prices AS sp
JOIN dbo.products AS p ON p.product_id = sp.product_id;
"""


class LogGrowth(Incident):
    name = "log-growth"
    title = "Log growth: an open transaction blocks log reuse"
    runs_on = ("vm",)
    default_minutes = 30
    max_minutes = 45
    root_cause = ("A long-running open transaction from an interactive SSMS session (the SQL admin), repeatedly "
                  "reloading vendor.supplier_prices_staging, prevents SalesDB's transaction log from being reused "
                  "(log_reuse_wait ACTIVE_TRANSACTION): log backups run but free nothing, so the log fills and grows.")
    correct_fix = ("Find the oldest open transaction (DBCC OPENTRAN, sys.dm_tran_database_transactions / "
                   "session_transactions), have it committed or rolled back, or KILL the session; then the next log "
                   "backup frees the space (shrink the log only if it grew far beyond its normal size). "
                   "Prevention: no manual work inside long open transactions on production.")
    expected_signals = [
        "alert shoplite-sql-log-full (LogUsedPercent over 70) and possibly log growth (LogSizeMB rising)",
        "SqlDatabases_CL: SalesDB LogReuseWait ACTIVE_TRANSACTION",
        "SqlSessions_CL / SqlBlocking_CL: an SSMS session with an open transaction for a long time",
        "SqlBackups_CL: log backups keep running but grow larger",
        "SqlFileIo_CL: write activity on SalesDB_log",
    ]

    def __init__(self):
        self.connection = None
        self.start_size_mb = None

    def preflight(self, ctx: Context) -> None:
        if ctx.settings.environment == "prod":
            now = datetime.now(timezone.utc)
            etl_start = now.replace(hour=13, minute=55, second=0, microsecond=0)
            etl_end = now.replace(hour=14, minute=20, second=0, microsecond=0)
            if now < etl_end and ctx.deadline > etl_start:
                raise Refused("It would overlap the 14:00 vendor ETL, which uses the staging table. "
                              "Start after 14:20 UTC.")

    def _log_state(self, ctx: Context):
        connection = ctx.sql(PROGRAM)
        try:
            size_mb, used = connection.cursor().execute(LOG_STATE).fetchone()
            return float(size_mb), float(used)
        finally:
            connection.close()
            ctx.sessions.pop()

    def start(self, ctx: Context) -> None:
        self.start_size_mb, used = self._log_state(ctx)
        ctx.details["log_size_mb_at_start"] = round(self.start_size_mb)
        ctx.note(f"SalesDB log: {self.start_size_mb:.0f} MB, {used:.0f}% used")

        def open_transaction():
            self.connection = ctx.sql(PROGRAM, autocommit=False)
            self.connection.timeout = 120
            self.connection.cursor().execute(RELOAD)        # the transaction starts here and stays open
        ctx.do("as the SQL admin in 'SSMS', open a transaction that reloads the staging table, and keep "
               f"reloading it every {CYCLE_SECONDS} s without committing (pausing above "
               f"{PAUSE_ABOVE_PERCENT}% log use, stopping after {MAX_GROWTH_MB} MB of growth)", open_transaction)

    def hold(self, ctx: Context) -> None:
        growing = True
        while not ctx.should_stop():
            size_mb, used = self._log_state(ctx)
            if growing and size_mb - self.start_size_mb >= MAX_GROWTH_MB:
                growing = False
                ctx.note(f"log grew {size_mb - self.start_size_mb:.0f} MB: no more writes "
                         "(the transaction stays open until the end)")
            if growing and used < PAUSE_ABOVE_PERCENT:
                self.connection.cursor().execute(RELOAD)
            ctx.note(f"log {size_mb:.0f} MB, {used:.0f}% used" + ("" if growing else " (writes stopped)"))
            ctx.wait(CYCLE_SECONDS)

    def revert(self, ctx: Context) -> None:
        if self.connection is None:
            ctx.note("no open session from this run (if the injector died, SQL Server already rolled it back)")
            return

        def rollback():
            try:
                self.connection.timeout = 0                 # rolling back can take a while: let it finish
                self.connection.rollback()
            finally:
                self.connection.close()
                self.connection = None
        ctx.do("roll back the open transaction (the next log backup then frees the log)", rollback)


INCIDENT = LogGrowth()
