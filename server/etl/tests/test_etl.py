"""Tests for the vendor ETL, against your LOCAL Docker databases only.

Run from the repo root, with the ETL's virtual environment:

    server/etl/.venv/bin/python -m pytest server/etl/tests -v

Needs (README.md, "Local"): vendor-pg schema and seed, SalesDB with
server/etl/sql 001 and 002 applied, and the root .env (SHOPLITE_ETL_PASSWORD,
SHOPLITE_READER_PASSWORD; also the local admin logins, used only to build
scratch data). No server/etl/.env needed: that's the VM's file.
A test whose database isn't reachable is SKIPPED with the reason, not failed.
Any non-localhost setting stops the whole run: never point these at production.

What each group touches:
  - config         nothing (pure Python).
  - contract       a scratch copy of the vendor tables, built inside a
                   transaction that is always rolled back: the real supply
                   tables are never changed.
  - merge          SalesDB inside a transaction that is always rolled back:
                   vendor.supplier_prices looks untouched afterwards.
  - login          read-only checks as shoplite_etl.
  - end to end     REALLY runs the ETL twice, committing to your local
                   SalesDB like a normal run would. Skip it with
                   -k "not end_to_end".
"""

import copy
import dataclasses
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import pyodbc
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

ETL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = ETL_DIR.parent.parent
sys.path.insert(0, str(ETL_DIR))             # so the tests can import the ETL modules

import run_etl  # noqa: E402
from contract import validate  # noqa: E402

# Layered settings, exactly as the ETL reads them: server/etl/.env if present,
# then the root .env (which also has the local admin logins: SA_PASSWORD, POSTGRES_*).
sys.path.insert(0, str(REPO_ROOT / "tools"))
from envconfig import load_layers  # noqa: E402
load_layers(ETL_DIR)

LOCAL_HOSTS = {"localhost", "127.0.0.1"}
SCRATCH = "supply_contract_test"
TEST_SUPPLIER = "Test Supplier (pytest)"


def require_local(host: str, what: str) -> None:
    if host not in LOCAL_HOSTS:
        pytest.exit(f"{what} points at '{host}'. These tests run against local Docker only.", returncode=2)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def contract():
    return run_etl.load_contract(run_etl.DEFAULT_CONTRACT)


@pytest.fixture(scope="session")
def settings():
    try:
        loaded = run_etl.load_settings()
    except run_etl.ConfigError as error:
        pytest.skip(f"ETL settings (root .env, or server/etl/.env if present): {error}")
    require_local(loaded.salesdb_host, "SALESDB_HOST")
    require_local(conninfo_to_dict(loaded.vendor_url).get("host", "localhost"), "VENDOR_READER_URL")
    return loaded


@pytest.fixture
def vendor_reader(settings):
    """The real vendor, as shoplite_reader, exactly as the ETL connects."""
    try:
        conn = run_etl.connect_vendor(settings)
    except psycopg.OperationalError as error:
        pytest.skip(f"vendor-pg not reachable as shoplite_reader: {error}")
    with conn:
        yield conn


@pytest.fixture
def vendor_admin():
    """The local vendor admin, in ONE transaction that is rolled back at the end."""
    names = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    if not all(os.getenv(n) for n in names):
        pytest.skip(f"root .env is missing one of {', '.join(names)}")
    try:
        conn = psycopg.connect(host="localhost", port=5433, user=os.getenv("POSTGRES_USER"),
                               password=os.getenv("POSTGRES_PASSWORD"), dbname=os.getenv("POSTGRES_DB"),
                               connect_timeout=5)
    except psycopg.OperationalError as error:
        pytest.skip(f"vendor-pg not reachable as the admin: {error}")
    try:
        yield conn
    finally:
        conn.rollback()                      # throws away the scratch schema and every change
        conn.close()


@pytest.fixture
def scratch(vendor_admin, contract):
    """A copy of the four supply tables in a scratch schema, plus a copy of
    the contract pointing at it. Postgres DDL is transactional, so the
    rollback in vendor_admin removes it all.

    LIKE ... INCLUDING ALL copies columns, types, NOT NULL, checks, and keys,
    but not triggers (so tests can set updated_at freely). EXCLUDING IDENTITY
    lets the existing IDs be copied as plain values.
    """
    with vendor_admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCRATCH)))
        for table in contract["tables"]:
            source, target = sql.Identifier("supply", table), sql.Identifier(SCRATCH, table)
            cur.execute(sql.SQL("CREATE TABLE {} (LIKE {} INCLUDING ALL EXCLUDING IDENTITY)")
                        .format(target, source))
            cur.execute(sql.SQL("INSERT INTO {} SELECT * FROM {}").format(target, source))
        # Fresh no matter when you last ran activity.py: the breaks below are
        # then the only problems.
        cur.execute(sql.SQL("UPDATE {} SET updated_at = now()").format(sql.Identifier(SCRATCH, "prices")))
    scratch_contract = copy.deepcopy(contract)
    scratch_contract["source"]["schema"] = SCRATCH
    return vendor_admin, scratch_contract


def connect_salesdb_as(settings, user, password):
    try:
        return run_etl.connect_salesdb(
            dataclasses.replace(settings, salesdb_user=user, salesdb_password=password))
    except pyodbc.Error as error:
        pytest.skip(f"SalesDB not reachable as {user}: {run_etl.describe(error)}")


@pytest.fixture
def salesdb_admin(settings):
    """SalesDB as the local SQL admin, in ONE transaction rolled back at the end."""
    password = os.getenv("SA_PASSWORD")
    if not password:
        pytest.skip("root .env has no SA_PASSWORD")
    conn = connect_salesdb_as(settings, os.getenv("SQL_ADMIN_USER") or "sa", password)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# Config (no databases)
# ---------------------------------------------------------------------------
def test_config_refuses_a_login_other_than_the_reader(contract):
    with pytest.raises(run_etl.ConfigError, match="read-only login"):
        run_etl.check_vendor_url("postgresql://vendor:x@localhost:5433/vendordb", contract)


def test_config_refuses_a_hosted_vendor_without_tls(contract):
    with pytest.raises(run_etl.ConfigError, match="sslmode=require"):
        run_etl.check_vendor_url("postgresql://shoplite_reader:x@example.neon.tech/vendordb", contract)


def test_config_accepts_local_and_tls_urls(contract):
    run_etl.check_vendor_url("postgresql://shoplite_reader:x@localhost:5433/vendordb", contract)
    run_etl.check_vendor_url(
        "postgresql://shoplite_reader:x@example.neon.tech/vendordb?sslmode=require", contract)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
def break_and_validate(scratch, statement: str) -> list[str]:
    """Run one change on the scratch copy ({s} = its schema), then validate."""
    conn, scratch_contract = scratch
    with conn.cursor() as cur:
        cur.execute(sql.SQL(statement).format(s=sql.Identifier(SCRATCH)))
    return validate(conn, scratch_contract).problems


def assert_reported(problems: list[str], *fragments: str) -> None:
    assert any(all(f in p for f in fragments) for p in problems), \
        f"expected a problem mentioning {fragments}, got: {problems}"


def test_real_vendor_meets_the_contract(vendor_reader, contract):
    # Fails with "hours old" if vendor activity hasn't run in 36 hours:
    # run python vendor/activity.py, as the real feed would be stale too.
    result = validate(vendor_reader, contract)
    assert result.ok, result.problems
    assert result.newest_change_at is not None


def test_unchanged_copy_meets_the_contract(scratch):
    conn, scratch_contract = scratch
    assert validate(conn, scratch_contract).problems == []


def test_renamed_column_is_reported(scratch):
    problems = break_and_validate(scratch, "ALTER TABLE {s}.prices RENAME COLUMN unit_cost TO cost")
    assert_reported(problems, "prices.unit_cost", "column missing")


def test_changed_type_is_reported(scratch):
    problems = break_and_validate(scratch, "ALTER TABLE {s}.prices ALTER COLUMN unit_cost TYPE double precision")
    assert_reported(problems, "prices.unit_cost", "double precision")


def test_column_becoming_nullable_is_reported(scratch):
    problems = break_and_validate(scratch, "ALTER TABLE {s}.catalog_items ALTER COLUMN description DROP NOT NULL")
    assert_reported(problems, "catalog_items.description", "nullable")


def test_extra_column_is_allowed(scratch):
    assert break_and_validate(scratch, "ALTER TABLE {s}.prices ADD COLUMN list_price numeric(10,2)") == []


def test_stale_prices_are_reported(scratch):
    problems = break_and_validate(scratch, "UPDATE {s}.prices SET updated_at = now() - interval '40 hours'")
    assert_reported(problems, "prices", "hours old")


def test_bad_sku_is_reported(scratch):
    problems = break_and_validate(
        scratch,
        "UPDATE {s}.catalog_items SET shoplite_sku = 'bad-sku' "
        "WHERE catalog_item_id = (SELECT min(catalog_item_id) FROM {s}.catalog_items)")
    assert_reported(problems, "shoplite_sku", "bad-sku")


def test_missing_stock_rows_are_reported(scratch):
    problems = break_and_validate(
        scratch, "DELETE FROM {s}.stock WHERE catalog_item_id IN (SELECT catalog_item_id FROM {s}.stock LIMIT 5)")
    assert_reported(problems, "5 rows have no matching row", "stock")


def test_missing_supplier_is_reported(scratch):
    problems = break_and_validate(
        scratch, "DELETE FROM {s}.suppliers WHERE supplier_id = (SELECT min(supplier_id) FROM {s}.suppliers)")
    assert_reported(problems, "catalog_items.supplier_id", "missing")


def test_too_few_rows_are_reported(scratch):
    problems = break_and_validate(scratch, "DELETE FROM {s}.prices")
    assert_reported(problems, "prices: 0 rows", "contract minimum")


# ---------------------------------------------------------------------------
# Merge (as the admin, everything rolled back)
# ---------------------------------------------------------------------------
def stage_rows(conn, rows) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM vendor.supplier_prices_staging;")
    cur.executemany(run_etl.STAGE_SQL, rows)


def test_merge_inserts_is_idempotent_updates_and_discontinues(salesdb_admin):
    cur = salesdb_admin.cursor()
    # Start from an empty target (inside the transaction), so "missing from
    # the feed" affects only these test rows.
    cur.execute("DELETE FROM vendor.supplier_prices;")
    skus = [row[0] for row in cur.execute("SELECT TOP 3 sku FROM dbo.products ORDER BY product_id;").fetchall()]
    assert len(skus) == 3, "SalesDB has no products: run shoplite/db/seed.py"
    rows = [(TEST_SUPPLIER, f"T-{i}", sku, Decimal("10.00"), 100, False) for i, sku in enumerate(skus)]
    unknown = (TEST_SUPPLIER, "T-unknown", "ZZ-99999", Decimal("1.00"), 1, False)

    # 1. First load: three new rows, the unknown SKU skipped.
    stage_rows(salesdb_admin, rows + [unknown])
    counts = run_etl.merge(salesdb_admin, 1)
    assert (counts["rows_inserted"], counts["rows_skipped"], counts["rows_updated"]) == (3, 1, 0)

    # 2. The same feed again changes nothing: idempotent.
    stage_rows(salesdb_admin, rows + [unknown])
    counts = run_etl.merge(salesdb_admin, 1)
    assert (counts["rows_inserted"], counts["rows_updated"], counts["rows_unchanged"]) == (0, 0, 3)

    # 3. One cost changes, one row vanishes from the feed.
    changed = (TEST_SUPPLIER, "T-0", skus[0], Decimal("12.50"), 100, False)
    stage_rows(salesdb_admin, [changed, rows[1], unknown])
    counts = run_etl.merge(salesdb_admin, 1)
    assert (counts["rows_updated"], counts["rows_discontinued"], counts["rows_unchanged"]) == (1, 1, 1)
    vanished = cur.execute("SELECT is_discontinued, supplier_stock, discontinued_at FROM vendor.supplier_prices "
                           "WHERE supplier_name = ? AND supplier_sku = 'T-2';", TEST_SUPPLIER).fetchone()
    assert (vanished[0], vanished[1]) == (True, 0) and vanished[2] is not None   # kept, flagged, stock 0

    # 4. The vendor flags an item discontinued itself: counted as discontinued, not updated.
    flagged = (TEST_SUPPLIER, "T-1", skus[1], Decimal("10.00"), 100, True)
    stage_rows(salesdb_admin, [changed, flagged, unknown])
    counts = run_etl.merge(salesdb_admin, 1)
    assert (counts["rows_discontinued"], counts["rows_updated"], counts["rows_unchanged"]) == (1, 0, 1)


def test_merge_refuses_too_few_staged_rows(salesdb_admin):
    sku = salesdb_admin.cursor().execute("SELECT TOP 1 sku FROM dbo.products;").fetchone()[0]
    stage_rows(salesdb_admin, [(TEST_SUPPLIER, "T-0", sku, Decimal("10.00"), 1, False)])
    with pytest.raises(pyodbc.Error, match="Refusing to merge"):
        run_etl.merge(salesdb_admin, 5000)


# ---------------------------------------------------------------------------
# Login: shoplite_etl has only what it needs
# ---------------------------------------------------------------------------
def test_etl_login_is_least_privilege(settings):
    conn = connect_salesdb_as(settings, settings.salesdb_user, settings.salesdb_password)
    try:
        cur = conn.cursor()
        cur.execute("SELECT TOP 1 run_id FROM ops.etl_runs;").fetchall()          # allowed
        for forbidden in ("SELECT TOP 1 * FROM dbo.orders;",
                          "SELECT TOP 1 * FROM vendor.supplier_prices;",
                          "DELETE FROM vendor.supplier_prices_staging;"):
            with pytest.raises(pyodbc.Error, match="permission was denied"):
                cur.execute(forbidden)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# End to end: the real ETL, twice (commits to local SalesDB)
# ---------------------------------------------------------------------------
def test_end_to_end_second_run_changes_nothing(vendor_reader, salesdb_admin):
    for attempt in (1, 2):
        result = subprocess.run([sys.executable, str(ETL_DIR / "run_etl.py")],
                                capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, f"run {attempt} exited {result.returncode}:\n{result.stdout}{result.stderr}"

    latest = salesdb_admin.cursor().execute(
        "SELECT TOP 1 status, rows_inserted, rows_updated, rows_discontinued, rows_unchanged "
        "FROM ops.etl_runs ORDER BY run_id DESC;").fetchone()
    assert latest[0] == "succeeded"
    assert (latest[1], latest[2], latest[3]) == (0, 0, 0), f"second run changed data: {latest}"
    assert latest[4] > 0
