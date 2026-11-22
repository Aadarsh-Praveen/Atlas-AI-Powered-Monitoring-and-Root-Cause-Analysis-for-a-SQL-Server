"""Database access for the ShopLite API: engine, pool, timeouts, session tag,
and helpers for reading SQL Server errors.

Routers use the engine like this:
    with get_engine().connect() as conn:      # read-only work
        rows = conn.execute(text("SELECT ..."), {"id": 5}).all()

    with get_engine().begin() as conn:        # one transaction: commits on
        conn.execute(text("UPDATE ..."), ...) # success, rolls back on error

SQL is written by hand with text() and named parameters (:id), never by
pasting values into strings. Parameters are sent separately from the SQL,
which prevents SQL injection and lets SQL Server reuse query plans.
"""

import logging
import re
from dataclasses import dataclass

import pyodbc
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError

from app import telemetry
from app.config import Settings, get_settings

logger = logging.getLogger("shoplite.db")

ODBC_DRIVER = "ODBC Driver 18 for SQL Server"

# Let SQLAlchemy be the only connection pool. pyodbc can also ask the ODBC
# driver manager to pool connections (on by default on Windows, where the
# API runs from Phase 2); two pools stacked on each other make connection
# counts and resets hard to reason about. Must be set before connecting.
pyodbc.pooling = False


# ===========================================================================
# Engine
# ===========================================================================

def build_connection_string(settings: Settings) -> str:
    """ODBC connection string for SalesDB as the shoplite_app login.

    Same parts as tools/check_local.py, plus APP: the program name SQL Server
    records for each session (sys.dm_exec_sessions.program_name). With the
    version in it, Atlas can tell which release a blocking session came from.
    """
    # Inside {...} a literal "}" must be doubled.
    password = settings.db_password.get_secret_value().replace("}", "}}")
    parts = [
        f"DRIVER={{{ODBC_DRIVER}}}",
        f"SERVER={settings.db_host},{settings.db_port}",
        f"DATABASE={settings.db_name}",
        f"UID={settings.db_user}",
        f"PWD={{{password}}}",
        "Encrypt=yes",  # always encrypted; only certificate trust is configurable
        f"TrustServerCertificate={'yes' if settings.db_trust_server_certificate else 'no'}",
        f"APP={settings.session_tag}",
    ]
    return ";".join(parts) + ";"


def create_db_engine(settings: Settings) -> Engine:
    """Create the SQLAlchemy engine and its connection pool."""
    url = URL.create("mssql+pyodbc", query={"odbc_connect": build_connection_string(settings)})

    engine = create_engine(
        url,
        # Pool: keep pool_size connections open for reuse; open up to
        # max_overflow more under load; a request waits pool_timeout seconds
        # for a free one before failing (sqlalchemy.exc.TimeoutError -> 503).
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        # Replace connections older than this, so none outlive a server-side
        # or network idle timeout.
        pool_recycle=settings.db_pool_recycle_seconds,
        # Before handing out a pooled connection, check it still works (for
        # example after SQL Server restarts) and replace it if not.
        pool_pre_ping=True,
        # Passed to pyodbc.connect(): the LOGIN timeout in seconds.
        connect_args={"timeout": settings.db_connect_timeout_seconds},
    )

    @event.listens_for(engine, "connect")
    def configure_new_connection(dbapi_connection, _connection_record):
        """Runs once for every new physical connection the pool opens."""
        # QUERY timeout: pyodbc cancels any statement that runs longer than
        # this. A query blocked behind someone else's locks fails instead of
        # hanging the request (and its pooled connection) forever.
        dbapi_connection.timeout = settings.db_query_timeout_seconds

        # XACT_ABORT ON: if any statement fails, or the client cancels it
        # (which is what a query timeout does), SQL Server rolls back the
        # whole transaction immediately. Without it, a timed-out transaction
        # can stay open on the server, still holding its locks.
        cursor = dbapi_connection.cursor()
        cursor.execute("SET XACT_ABORT ON;")
        cursor.close()

    logger.info(
        "Database engine: %s,%s/%s as %s (pool %d + %d overflow, query timeout %ds, tag %s)",
        settings.db_host, settings.db_port, settings.db_name, settings.db_user,
        settings.db_pool_size, settings.db_pool_max_overflow,
        settings.db_query_timeout_seconds, settings.session_tag,
    )
    # Every SQL call becomes an Application Insights dependency (Phase 5);
    # does nothing when telemetry is off.
    telemetry.instrument_engine(engine)
    return engine


_engine: Engine | None = None


def get_engine() -> Engine:
    """The process-wide engine, created on first use.

    One engine (and so one pool) per process. Creating an engine per request
    would open a new connection every time and defeat the pool.
    """
    global _engine
    if _engine is None:
        _engine = create_db_engine(get_settings())
    return _engine


def dispose_engine() -> None:
    """Close all pooled connections (called when the API shuts down)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


# ===========================================================================
# Reading SQL Server errors
# ===========================================================================

# Native SQL Server error numbers the API reacts to.
CHECK_OR_FK_CONFLICT = 547         # CHECK or FOREIGN KEY constraint violated
UNIQUE_INDEX_VIOLATION = 2601      # duplicate key in a unique index
UNIQUE_CONSTRAINT_VIOLATION = 2627 # duplicate key in a UNIQUE / PRIMARY KEY constraint
REPORT_INPUT_ERRORS = frozenset({50001, 50002, 50003})  # THROWn by 005_report_procedures.sql

# ODBC SQLSTATEs meaning "the database is unreachable or too slow right now".
QUERY_TIMEOUT_SQLSTATE = "HYT00"
CONNECTION_FAILURE_SQLSTATES = frozenset({"08001", "08S01", "HYT00", "HYT01"})

# pyodbc's error text looks like:
#   [23000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]The UPDATE
#   statement conflicted with the CHECK constraint "CK_inventory_quantity". ...
#   (547) (SQLExecDirectW)
# The server's own message follows "[SQL Server]" and its native error number
# is the first "(digits)" after it.
SQL_SERVER_MESSAGE = re.compile(r"\[SQL Server\](?P<message>.*?)\s\((?P<number>\d+)\)", re.DOTALL)


@dataclass(frozen=True)
class SqlError:
    sqlstate: str | None   # five-character ODBC code, e.g. '23000', 'HYT00'
    number: int | None     # SQL Server's native error number, e.g. 547, 50001
    message: str           # the server's message without the driver prefixes


def describe_sql_error(error: DBAPIError) -> SqlError:
    """Pull the SQLSTATE, native error number, and clean message out of an error.

    SQLAlchemy wraps the pyodbc error; the original is error.orig, whose args
    are (sqlstate, full_text).
    """
    args = getattr(error.orig, "args", ())
    sqlstate = args[0] if args and isinstance(args[0], str) else None
    full_text = args[1] if len(args) > 1 else str(error.orig)

    match = SQL_SERVER_MESSAGE.search(full_text)
    if match:
        return SqlError(sqlstate, int(match["number"]), match["message"].strip())
    # Driver-level errors (timeouts, lost connections) have no [SQL Server] part.
    return SqlError(sqlstate, None, full_text)


def is_report_input_error(error: DBAPIError) -> bool:
    """A report procedure rejected its parameters (THROW 50001-50003)."""
    return describe_sql_error(error).number in REPORT_INPUT_ERRORS


def violates_check_constraint(error: DBAPIError, constraint_name: str) -> bool:
    """A specific CHECK constraint was violated, e.g. 'CK_inventory_quantity'.

    Error 547 covers both CHECK and FOREIGN KEY conflicts, so the constraint
    name in the message is what tells them apart.
    """
    details = describe_sql_error(error)
    return details.number == CHECK_OR_FK_CONFLICT and f'"{constraint_name}"' in details.message


def violates_unique(error: DBAPIError, index_or_constraint_name: str) -> bool:
    """A specific unique index or constraint was violated, e.g. 'UX_carts_customer_open'."""
    details = describe_sql_error(error)
    return (details.number in (UNIQUE_INDEX_VIOLATION, UNIQUE_CONSTRAINT_VIOLATION)
            and index_or_constraint_name in details.message)


def is_database_unavailable(error: DBAPIError) -> bool:
    """The server couldn't be reached, the connection dropped, or a query timed out."""
    return (error.connection_invalidated
            or describe_sql_error(error).sqlstate in CONNECTION_FAILURE_SQLSTATES)
