"""Check that Python can connect to the local development databases.

Connects to SQL Server (container shoplite-sql) and Postgres (container
vendor-pg), prints each server's version and current time, and explains the
most likely cause when a connection fails.

Run from the repo root, inside your virtual environment:
    python tools/check_local.py

Exit code is 0 if both databases connect, 1 otherwise.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# The root .env sits one folder above tools/. Building the path from this
# file's location means the script works no matter which folder you run it from.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

# These match the host side of the port mappings in docker-compose.yml. They
# are not secrets, so they live in code rather than .env.
HOST = "localhost"
SQL_PORT = 1433
# 5433, not 5432: a locally installed Postgres owns 5432 on this Mac, and
# connecting there would reach the wrong server.
PG_PORT = 5433

# Must match the driver name exactly as installed (check with: odbcinst -q -d).
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"

REQUIRED_VARS = ["SA_PASSWORD", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]

# Give up after 5 seconds instead of hanging when a container isn't running.
TIMEOUT_SECONDS = 5


def load_settings():
    """Load .env and return its values, or exit with a clear message."""
    if not ENV_FILE.exists():
        print(f"[FAIL] No .env file at {ENV_FILE}")
        print("       Likely cause: it hasn't been created yet. Run: cp .env.example .env")
        sys.exit(1)

    load_dotenv(ENV_FILE)

    missing = [name for name in REQUIRED_VARS if not os.getenv(name)]
    if missing:
        print(f"[FAIL] Missing values in .env: {', '.join(missing)}")
        print("       Likely cause: a line was deleted or left blank. Compare with .env.example.")
        sys.exit(1)

    return {name: os.getenv(name) for name in REQUIRED_VARS}


def check_sql_server(settings):
    """Connect to SQL Server with pyodbc. Returns True on success."""
    import pyodbc  # imported here so a missing package only fails this check

    # pyodbc can only use drivers registered with unixODBC on this Mac.
    if ODBC_DRIVER not in pyodbc.drivers():
        print(f"[FAIL] SQL Server: '{ODBC_DRIVER}' is not installed.")
        print("       Likely cause: msodbcsql18 is missing. Install it from Microsoft's Homebrew tap.")
        return False

    # In an ODBC connection string, wrapping a value in {} lets it contain
    # characters like ";". A "}" inside the value must be doubled to "}}".
    password = settings["SA_PASSWORD"].replace("}", "}}")

    # Each part of the connection string, and why it's there:
    #   DRIVER                  which ODBC driver handles the connection
    #   SERVER                  host,port (SQL Server uses a comma, not a colon)
    #   DATABASE                master always exists; ShopLite's SalesDB comes in Phase 1
    #   UID / PWD               SQL login; sa is fine locally, never for Atlas
    #   Encrypt=yes             ODBC Driver 18 encrypts by default; stated explicitly
    #   TrustServerCertificate  the container uses a self-signed certificate, which
    #                           can't be verified, so we trust it. Local only:
    #                           production will use a properly trusted certificate.
    conn_str = (
        f"DRIVER={{{ODBC_DRIVER}}};"
        f"SERVER={HOST},{SQL_PORT};"
        "DATABASE=master;"
        "UID=sa;"
        f"PWD={{{password}}};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
    )

    try:
        # timeout= is the login timeout in seconds.
        with pyodbc.connect(conn_str, timeout=TIMEOUT_SECONDS) as conn:
            row = conn.cursor().execute("SELECT @@VERSION, SYSDATETIME();").fetchone()
    except pyodbc.Error as error:
        # pyodbc errors carry an ODBC SQLSTATE code as their first argument.
        sqlstate = error.args[0] if error.args else ""
        print(f"[FAIL] SQL Server at {HOST},{SQL_PORT}: {error.args[-1] if error.args else error}")
        if sqlstate == "28000":
            print("       Likely cause: wrong password. SA_PASSWORD in .env doesn't match the")
            print("       one the container was first created with.")
        elif sqlstate in ("08001", "HYT00", "08S01"):
            print("       Likely cause: the container isn't running or isn't ready yet.")
            print("       Check: docker compose ps   (wait for shoplite-sql to be 'healthy')")
        elif sqlstate in ("IM002", "01000"):
            print("       Likely cause: the ODBC driver is missing or not registered.")
        else:
            print("       Check: docker compose logs shoplite-sql")
        return False

    # @@VERSION is several lines long; the first line names the version.
    version_line = row[0].splitlines()[0].strip()
    print(f"[OK]   SQL Server at {HOST},{SQL_PORT}")
    print(f"       Version: {version_line}")
    print(f"       Server time: {row[1]}")
    return True


def check_postgres(settings):
    """Connect to Postgres with psycopg. Returns True on success."""
    import psycopg

    try:
        # Keyword arguments instead of a URL, so special characters in the
        # password need no escaping.
        with psycopg.connect(
            host=HOST,
            port=PG_PORT,
            user=settings["POSTGRES_USER"],
            password=settings["POSTGRES_PASSWORD"],
            dbname=settings["POSTGRES_DB"],
            connect_timeout=TIMEOUT_SECONDS,
        ) as conn:
            row = conn.execute("SELECT version(), now();").fetchone()
    except psycopg.OperationalError as error:
        # Errors raised while connecting often have no SQLSTATE code attached
        # (sqlstate is None), so we also look at the message text. Codes:
        # 28P01 = invalid password, 3D000 = database does not exist.
        sqlstate = getattr(error, "sqlstate", None)
        text = str(error)
        print(f"[FAIL] Postgres at {HOST}:{PG_PORT}: {text.strip().splitlines()[-1]}")
        if sqlstate == "28P01" or "password authentication failed" in text:
            print("       Likely cause: wrong password. POSTGRES_PASSWORD in .env doesn't match")
            print("       the one the volume was first created with.")
        elif sqlstate == "3D000" or ("database" in text and "does not exist" in text):
            print("       Likely cause: POSTGRES_DB in .env was changed after the volume was created.")
        elif "Connection refused" in text or "timeout" in text.lower():
            print("       Likely cause: the container isn't running or isn't ready yet.")
            print("       Check: docker compose ps   (wait for vendor-pg to be 'healthy')")
        else:
            print("       Check: docker compose logs vendor-pg")
        return False

    print(f"[OK]   Postgres at {HOST}:{PG_PORT}")
    print(f"       Version: {row[0]}")
    print(f"       Server time: {row[1]}")
    return True


def main():
    settings = load_settings()

    # Run both checks even if the first fails, so you see every problem at once.
    sql_ok = check_sql_server(settings)
    print()
    pg_ok = check_postgres(settings)
    print()

    if sql_ok and pg_ok:
        print("Both local databases are reachable.")
        return 0
    print("One or more checks failed. See the likely causes above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
