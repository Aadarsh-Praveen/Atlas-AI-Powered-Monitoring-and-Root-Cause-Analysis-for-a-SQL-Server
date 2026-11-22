"""ShopLite's daily vendor ETL: extract -> validate -> stage -> merge.

Loads the vendor's supplier catalog, prices, and stock (Postgres, read-only
login shoplite_reader) into SalesDB's vendor.supplier_prices (as the
least-privilege login shoplite_etl). On prod-sql-01 a SQL Agent job runs it
on weekdays at about 14:00 UTC, after the vendor's 12:00 update; locally you
run it by hand:

    server/etl/.venv/bin/python server/etl/run_etl.py            # a real run
    server/etl/.venv/bin/python server/etl/run_etl.py --check    # validate only

Settings come in layers (tools/envconfig.py): real environment variables,
then server/etl/.env if present, then the repo's root .env. Locally the root
.env is enough: the SalesDB password falls back to SHOPLITE_ETL_PASSWORD, and
the vendor URL is built from SHOPLITE_READER_PASSWORD, VENDOR_HOST/PORT, and
POSTGRES_DB (encoded). On the VM server/etl/.env is the only source
(LOAD_ROOT_ENV=false).

A run, step by step (each step's name is what ops.etl_runs.error_stage says
when it fails):
  - SalesDB first: connect, take a lock so only one run works at a time, and
    insert a "running" row in ops.etl_runs. Without SalesDB nothing can be
    recorded, so that failure is only printed (exit code 3).
  - connect   to the vendor: TLS required for a hosted vendor, 15 s timeout.
  - validate  against the contract (contract.py), in a read-only
              REPEATABLE READ transaction: validation and extract see the
              same snapshot, even if the vendor changes data meanwhile.
  - extract   the joined feed (about 10,000 rows) into memory, then close the
              vendor connection.
  - stage     empty staging and bulk-insert the rows (one transaction).
  - merge     vendor.usp_merge_supplier_prices: one short transaction in
              SalesDB, returning the counts.
  - The row becomes "succeeded" with its counts, or "failed" with the stage
    and the error message.

Running it twice in a row is safe: the second run finds nothing changed.

Exit codes (SQL Agent's job history shows them):
    0  succeeded               11  vendor unreachable (connect)
    2  configuration problem   12  contract violated (validate)
    3  SalesDB unreachable     13  extract failed
    4  another run is active   14  staging failed
                               15  merge failed (or refused: too few rows)
"""

import argparse
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pyodbc
import yaml
from psycopg.conninfo import conninfo_to_dict

from contract import load_contract, validate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))
from envconfig import load_layers, vendor_url  # noqa: E402

ETL_DIR = Path(__file__).resolve().parent
REPO_ROOT = ETL_DIR.parent.parent
DEFAULT_CONTRACT = REPO_ROOT / "knowledge" / "contracts" / "vendor-supplier-feed.yml"
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TLS_MODES = {"require", "verify-ca", "verify-full"}
LOCK_NAME = "vendor-supplier-feed-etl"
MAX_ERROR_MESSAGE = 2000                     # ops.etl_runs.error_message is NVARCHAR(2000)

EXIT_OK, EXIT_CONFIG, EXIT_SALESDB, EXIT_ALREADY_RUNNING = 0, 2, 3, 4
EXIT_FOR_STAGE = {"connect": 11, "validate": 12, "extract": 13, "stage": 14, "merge": 15}


class ConfigError(Exception):
    pass


class ContractViolation(Exception):
    pass


def log(message: str) -> None:
    # UTC timestamps, flushed at once: SQL Agent keeps the step's output.
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {message}", flush=True)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    vendor_url: str
    vendor_connect_timeout: int
    vendor_statement_timeout: int
    salesdb_host: str
    salesdb_port: int
    salesdb_name: str
    salesdb_user: str
    salesdb_password: str
    salesdb_trust_certificate: bool
    salesdb_connect_timeout: int
    salesdb_query_timeout: int


def setting(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if default is not None:
        return default
    raise ConfigError(f"{name} is not set (see server/etl/.env.example)")


def required_password() -> str:
    """SALESDB_PASSWORD (server/etl/.env, e.g. on the VM), or the root .env's
    canonical SHOPLITE_ETL_PASSWORD (locally)."""
    for name in ("SALESDB_PASSWORD", "SHOPLITE_ETL_PASSWORD"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise ConfigError("No SalesDB password: set SALESDB_PASSWORD in server/etl/.env, "
                      "or SHOPLITE_ETL_PASSWORD in the root .env")


def required_vendor_url() -> str:
    """VENDOR_READER_URL exactly as given (Neon), or built from the root .env's
    parts (shoplite_reader, SHOPLITE_READER_PASSWORD, VENDOR_HOST/PORT, POSTGRES_DB),
    with the password percent-encoded."""
    url = vendor_url("reader", explicit="VENDOR_READER_URL")
    if not url:
        raise ConfigError("No vendor URL: set VENDOR_READER_URL in server/etl/.env, or "
                          "SHOPLITE_READER_PASSWORD and POSTGRES_DB in the root .env")
    return url


def number(name: str, default: int) -> int:
    value = setting(name, str(default))
    if not value.isdigit() or int(value) <= 0:
        raise ConfigError(f"{name} must be a whole number of seconds, found '{value}'")
    return int(value)


def flag(name: str, fallback: str | None = None, default: str | None = None) -> bool:
    """true/false from NAME, else from FALLBACK (a root .env name), else DEFAULT."""
    value = (os.environ.get(name, "").strip()
             or (os.environ.get(fallback, "").strip() if fallback else "")
             or default)
    if value is None:
        raise ConfigError(f"{name} is not set (see server/etl/.env.example)")
    value = value.lower()
    if value not in ("true", "false"):
        raise ConfigError(f"{name} must be true or false, found '{value}'")
    return value == "true"


def load_settings() -> Settings:
    return Settings(
        vendor_url=required_vendor_url(),
        vendor_connect_timeout=number("VENDOR_CONNECT_TIMEOUT_SECONDS", 15),
        vendor_statement_timeout=number("VENDOR_STATEMENT_TIMEOUT_SECONDS", 120),
        salesdb_host=setting("SALESDB_HOST", "localhost"),
        salesdb_port=number("SALESDB_PORT", 1433),
        salesdb_name=setting("SALESDB_NAME", "SalesDB"),
        salesdb_user=setting("SALESDB_USER", "shoplite_etl"),
        salesdb_password=required_password(),
        # Own name, else the root .env's SQL_TRUST_SERVER_CERTIFICATE, else true: the
        # ETL only ever connects to SQL Server on the same machine (the VM, or the
        # local container), whose certificate is self-signed. The VM's .env sets it.
        salesdb_trust_certificate=flag("SALESDB_TRUST_SERVER_CERTIFICATE",
                                       fallback="SQL_TRUST_SERVER_CERTIFICATE", default="true"),
        salesdb_connect_timeout=number("SALESDB_CONNECT_TIMEOUT_SECONDS", 10),
        salesdb_query_timeout=number("SALESDB_QUERY_TIMEOUT_SECONDS", 300),
    )


def check_vendor_url(url: str, contract: dict) -> None:
    """Refuse a vendor URL that breaks the contract's source rules. The URL
    holds a password, so it never appears in a message."""
    try:
        params = conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        raise ConfigError("VENDOR_READER_URL is not a valid Postgres connection URL") from None
    expected_login = contract["source"]["login"]
    if params.get("user") != expected_login:
        # Stops the vendor admin (or any writer) connection being used by mistake.
        raise ConfigError(f"VENDOR_READER_URL must use the read-only login '{expected_login}'")
    host = params.get("host", "localhost")
    if host not in LOCAL_HOSTS and params.get("sslmode") not in TLS_MODES:
        raise ConfigError("VENDOR_READER_URL must end in ?sslmode=require: the contract requires "
                          "TLS for the hosted vendor")


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
def connect_vendor(settings: Settings) -> psycopg.Connection:
    conn = psycopg.connect(
        settings.vendor_url,
        connect_timeout=settings.vendor_connect_timeout,       # Neon may take seconds to wake
        options=f"-c statement_timeout={settings.vendor_statement_timeout * 1000}",
        application_name="ShopLite-ETL",                       # visible to the vendor in pg_stat_activity
    )
    # One read-only snapshot for validation AND extract.
    conn.read_only = True
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    return conn


def connect_salesdb(settings: Settings) -> pyodbc.Connection:
    def braced(value: str) -> str:
        # Braces make ODBC take the value literally; a "}" inside is doubled.
        return "{" + value.replace("}", "}}") + "}"

    conn_str = (
        f"DRIVER={{{ODBC_DRIVER}}};"
        f"SERVER={settings.salesdb_host},{settings.salesdb_port};"
        f"DATABASE={settings.salesdb_name};"
        f"UID={braced(settings.salesdb_user)};"
        f"PWD={braced(settings.salesdb_password)};"
        "Encrypt=yes;"
        f"TrustServerCertificate={'yes' if settings.salesdb_trust_certificate else 'no'};"
        "APP=ShopLite-ETL;"          # program_name in SQL Server: how Atlas tells ETL sessions apart
    )
    # autocommit: each statement commits on its own; staging opens its own
    # transaction, and the merge procedure manages its own.
    conn = pyodbc.connect(conn_str, timeout=settings.salesdb_connect_timeout, autocommit=True)
    conn.timeout = settings.salesdb_query_timeout             # per query, in seconds
    return conn


# ---------------------------------------------------------------------------
# SalesDB: lock and run log
# ---------------------------------------------------------------------------
LOCK_SQL = """
    SET NOCOUNT ON;
    DECLARE @result INT;
    EXEC @result = sp_getapplock @Resource = ?, @LockMode = 'Exclusive',
                                 @LockOwner = 'Session', @LockTimeout = 0;
    SELECT @result;
"""


def take_lock(salesdb) -> bool:
    """One run at a time (the job and a manual run could overlap, and two
    runs would empty each other's staging). The lock belongs to this
    session, so it's released when the connection closes, even after a crash.
    sp_getapplock returns 0 or 1 when granted, a negative number when not."""
    return salesdb.cursor().execute(LOCK_SQL, LOCK_NAME).fetchone()[0] >= 0


def start_run(salesdb, contract_version: str) -> int:
    row = salesdb.cursor().execute(
        "SET NOCOUNT ON; INSERT INTO ops.etl_runs (contract_version, run_host) "
        "OUTPUT inserted.run_id VALUES (?, ?);",
        contract_version, socket.gethostname()).fetchone()
    return row[0]


FINISH_SQL = """
    UPDATE ops.etl_runs SET
        finished_at = SYSUTCDATETIME(), status = ?,
        rows_read = ?, rows_skipped = ?, rows_inserted = ?, rows_updated = ?,
        rows_discontinued = ?, rows_unchanged = ?, vendor_newest_price_at = ?,
        error_stage = ?, error_message = ?
    WHERE run_id = ?
"""


def finish_run(salesdb, run_id: int, status: str, facts: dict,
               error_stage: str | None = None, error_message: str | None = None) -> None:
    salesdb.cursor().execute(
        FINISH_SQL, status,
        facts.get("rows_read"), facts.get("rows_skipped"), facts.get("rows_inserted"),
        facts.get("rows_updated"), facts.get("rows_discontinued"), facts.get("rows_unchanged"),
        facts.get("vendor_newest_price_at"), error_stage, error_message, run_id)


# ---------------------------------------------------------------------------
# The four steps
# ---------------------------------------------------------------------------
# Matches the contract's target.mapping (version 1.0.0). A contract change
# changes this query in the same commit.
EXTRACT_SQL = """
    SELECT s.name, c.supplier_sku, c.shoplite_sku, p.unit_cost, st.quantity_available, c.is_discontinued
    FROM supply.catalog_items AS c
    JOIN supply.suppliers     AS s  ON s.supplier_id = c.supplier_id
    JOIN supply.prices        AS p  ON p.catalog_item_id = c.catalog_item_id
    JOIN supply.stock         AS st ON st.catalog_item_id = c.catalog_item_id
    ORDER BY s.name, c.supplier_sku
"""

STAGE_SQL = """
    INSERT INTO vendor.supplier_prices_staging
        (supplier_name, supplier_sku, shoplite_sku, unit_cost, supplier_stock, is_discontinued)
    VALUES (?, ?, ?, ?, ?, ?)
"""


def extract(vendor) -> list[tuple]:
    with vendor.cursor() as cur:
        cur.execute(EXTRACT_SQL)
        return cur.fetchall()


def stage(salesdb, rows: list[tuple]) -> None:
    cur = salesdb.cursor()
    cur.execute("EXEC vendor.usp_clear_supplier_prices_staging;")
    # fast_executemany sends the rows in large batches instead of one round
    # trip per row (seconds instead of minutes for 10,000 rows). One
    # transaction: staging holds the whole extract or nothing.
    salesdb.autocommit = False
    try:
        cur.fast_executemany = True
        cur.executemany(STAGE_SQL, rows)
        salesdb.commit()
    except Exception:
        salesdb.rollback()
        raise
    finally:
        salesdb.autocommit = True


def merge(salesdb, min_rows: int) -> dict:
    cur = salesdb.cursor()
    cur.execute("EXEC vendor.usp_merge_supplier_prices @min_rows = ?;", min_rows)
    row = cur.fetchone()
    return {column[0]: value for column, value in zip(cur.description, row)}


def describe(error: Exception) -> str:
    # pyodbc puts the server's message last; the rest is driver detail.
    if isinstance(error, pyodbc.Error) and error.args:
        text = str(error.args[-1])
    else:
        text = str(error) or type(error).__name__
    return text[:MAX_ERROR_MESSAGE]


def to_utc_naive(moment: datetime | None) -> datetime | None:
    # SQL Server's DATETIME2 has no time zone; ops.etl_runs stores UTC.
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment else None


def validate_vendor(vendor, contract: dict, facts: dict) -> None:
    result = validate(vendor, contract)
    facts["vendor_newest_price_at"] = to_utc_naive(result.newest_change_at)
    for problem in result.problems:
        log(f"[CONTRACT] {problem}")
    if not result.ok:
        raise ContractViolation(f"Contract {contract['version']}: {len(result.problems)} problem(s): "
                                + "; ".join(result.problems))
    counts = ", ".join(f"{table} {count}" for table, count in result.row_counts.items())
    log(f"Contract {contract['version']} OK ({counts}).")


def run(salesdb, run_id: int, settings: Settings, contract: dict) -> int:
    facts: dict = {}
    current = "connect"
    started = time.monotonic()
    try:
        with connect_vendor(settings) as vendor:
            current = "validate"
            validate_vendor(vendor, contract, facts)
            current = "extract"
            rows = extract(vendor)
            facts["rows_read"] = len(rows)
            log(f"Extracted {len(rows)} rows from the vendor.")
        # The vendor connection is closed here: staging and merge don't need it.
        current = "stage"
        stage(salesdb, rows)
        current = "merge"
        facts.update(merge(salesdb, contract["volume"]["min_rows"]["catalog_items"]))
        finish_run(salesdb, run_id, "succeeded", facts)
        log(f"Run {run_id} succeeded in {time.monotonic() - started:.1f} s: read {facts['rows_read']}, "
            f"inserted {facts['rows_inserted']}, updated {facts['rows_updated']}, "
            f"discontinued {facts['rows_discontinued']}, unchanged {facts['rows_unchanged']}, "
            f"skipped {facts['rows_skipped']} (unknown SKUs).")
        return EXIT_OK
    except Exception as error:
        message = describe(error)
        log(f"[FAIL] Run {run_id} failed at '{current}': {message}")
        try:
            finish_run(salesdb, run_id, "failed", facts, current, message)
        except pyodbc.Error as log_error:
            # SalesDB went away mid-run: the row stays "running", which is
            # itself a signal (a run with no finished_at).
            log(f"[FAIL] Could not record the failure in ops.etl_runs: {describe(log_error)}")
        return EXIT_FOR_STAGE[current]


def check_only(settings: Settings, contract: dict) -> int:
    """--check: validate the vendor against the contract; SalesDB untouched."""
    try:
        with connect_vendor(settings) as vendor:
            validate_vendor(vendor, contract, {})
    except ContractViolation:
        return EXIT_FOR_STAGE["validate"]
    except psycopg.Error as error:
        log(f"[FAIL] Vendor: {describe(error)}")
        return EXIT_FOR_STAGE["connect"]
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Load the vendor supplier feed into SalesDB.")
    parser.add_argument("--check", action="store_true",
                        help="only validate the vendor against the contract; don't touch SalesDB")
    parser.add_argument("--env-file", type=Path, default=ETL_DIR / ".env",
                        help="the ETL's own settings file (default: server/etl/.env); the root .env "
                             "is read after it unless it says LOAD_ROOT_ENV=false")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT,
                        help="contract file (default: knowledge/contracts/vendor-supplier-feed.yml)")
    args = parser.parse_args()

    load_layers(own_env=args.env_file)  # own file, then the root .env; never overrides what's set
    try:
        settings = load_settings()
        contract = load_contract(args.contract)
        check_vendor_url(settings.vendor_url, contract)
    except (ConfigError, OSError, ValueError, yaml.YAMLError) as error:
        log(f"[FAIL] Configuration: {error}")
        return EXIT_CONFIG

    if args.check:
        return check_only(settings, contract)

    try:
        salesdb = connect_salesdb(settings)
    except pyodbc.Error as error:
        log(f"[FAIL] SalesDB unreachable, nothing recorded: {describe(error)}")
        return EXIT_SALESDB

    try:
        if not take_lock(salesdb):
            log("[FAIL] Another ETL run is in progress (lock held). Nothing done.")
            return EXIT_ALREADY_RUNNING
        run_id = start_run(salesdb, contract["version"])
        log(f"Run {run_id} started (contract {contract['version']}).")
        return run(salesdb, run_id, settings, contract)
    except pyodbc.Error as error:
        # The lock or the "running" row failed, e.g. the 001/002 scripts haven't run.
        log(f"[FAIL] SalesDB: {describe(error)}")
        return EXIT_SALESDB
    finally:
        salesdb.close()                 # also releases the lock


if __name__ == "__main__":
    sys.exit(main())
