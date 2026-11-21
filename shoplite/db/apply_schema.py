"""Build SalesDB by running the numbered schema scripts in order.

Runs every NNN_*.sql file in this folder (001, 002, ...) against the local
SQL Server as sa, one GO-separated batch at a time, and stops at the first
error.

!!! 001_create_database.sql DROPS and recreates SalesDB. All data is lost.
!!! Local development only.

Run from the repo root, inside the virtual environment:
    python shoplite/db/apply_schema.py

Exit code is 0 if every script succeeds, 1 otherwise.
"""

import os
import re
import sys
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

DB_DIR = Path(__file__).resolve().parent      # shoplite/db
REPO_ROOT = DB_DIR.parent.parent              # repo root, where .env lives
ENV_FILE = REPO_ROOT / ".env"

# Same local connection details as tools/check_local.py and docker-compose.yml.
HOST = "localhost"
SQL_PORT = 1433
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
LOGIN_TIMEOUT_SECONDS = 5

# Schema scripts are named like 001_create_database.sql. The three-digit
# prefix decides the order, so new scripts slot in by number.
SCRIPT_PATTERN = re.compile(r"^\d{3}_.+\.sql$")

# A line containing only "GO" (any case, optional surrounding spaces and an
# optional trailing -- comment) ends a batch. This mirrors sqlcmd. Note: like
# a simple sqlcmd, it doesn't know about comments, so a block comment must not
# contain a line that is only "GO".
GO_LINE = re.compile(r"^\s*GO\s*(--.*)?$", re.IGNORECASE)

# sqlcmd-style placeholders: $(NAME)
PLACEHOLDER = re.compile(r"\$\((\w+)\)")


def load_settings():
    """Load the root .env and return the values this script needs."""
    if not ENV_FILE.exists():
        sys.exit(f"[FAIL] No .env file at {ENV_FILE}. Run: cp .env.example .env")

    load_dotenv(ENV_FILE)
    settings = {
        "SA_PASSWORD": os.getenv("SA_PASSWORD"),
        "SHOPLITE_APP_PASSWORD": os.getenv("SHOPLITE_APP_PASSWORD"),
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        sys.exit(f"[FAIL] Missing values in .env: {', '.join(missing)}")
    return settings


def find_scripts():
    """Return the numbered .sql scripts in this folder, in order."""
    scripts = sorted(p for p in DB_DIR.iterdir() if SCRIPT_PATTERN.match(p.name))
    if not scripts:
        sys.exit(f"[FAIL] No NNN_*.sql scripts found in {DB_DIR}")
    return scripts


def substitute(sql, variables, script_name):
    """Replace $(NAME) placeholders with values, safely quoted.

    Values are used inside N'...' string literals, so any single quote is
    doubled ('') to keep it inside the string. Unknown placeholders stop the
    run rather than sending "$(NAME)" to SQL Server as a literal password.
    """
    def replace(match):
        name = match.group(1)
        if name not in variables:
            sys.exit(f"[FAIL] {script_name}: unknown placeholder $({name})")
        return variables[name].replace("'", "''")

    return PLACEHOLDER.sub(replace, sql)


def split_batches(sql):
    """Split a script on GO lines. Returns (starting_line_number, batch_text) pairs."""
    batches = []
    current = []
    start_line = 1
    for line_number, line in enumerate(sql.splitlines(), start=1):
        if GO_LINE.match(line):
            batches.append((start_line, "\n".join(current)))
            current = []
            start_line = line_number + 1
        else:
            current.append(line)
    batches.append((start_line, "\n".join(current)))  # text after the last GO

    # Drop batches that are empty or whitespace only.
    return [(start, text) for start, text in batches if text.strip()]


def connect(sa_password):
    """Connect to master as sa with autocommit on."""
    if ODBC_DRIVER not in pyodbc.drivers():
        sys.exit(f"[FAIL] '{ODBC_DRIVER}' is not installed. See Phase 0, Part A1.")

    # See tools/check_local.py for what each part means. APP names this
    # session in SQL Server's session views (sys.dm_exec_sessions.program_name).
    password = sa_password.replace("}", "}}")
    conn_str = (
        f"DRIVER={{{ODBC_DRIVER}}};"
        f"SERVER={HOST},{SQL_PORT};"
        "DATABASE=master;"
        "UID=sa;"
        f"PWD={{{password}}};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
        "APP=ShopLite-apply_schema;"
    )
    try:
        # autocommit=True: CREATE/DROP DATABASE and ALTER DATABASE can't run
        # inside a transaction, and pyodbc opens one by default. With
        # autocommit, each batch is committed as soon as it succeeds.
        return pyodbc.connect(conn_str, timeout=LOGIN_TIMEOUT_SECONDS, autocommit=True)
    except pyodbc.Error as error:
        print(f"[FAIL] Could not connect to SQL Server at {HOST},{SQL_PORT}: {error.args[-1]}")
        print("       Run 'python tools/check_local.py' for a diagnosis.")
        sys.exit(1)


def run_batch(cursor, batch):
    """Execute one batch and surface errors from every statement in it.

    pyodbc only raises an error for the statement whose results it is
    currently reading. In a batch with several statements, an error in a
    later one can go unnoticed unless every result is read. Looping over
    nextset() walks through all of them, so any error is raised here.
    """
    cursor.execute(batch)
    while cursor.nextset():
        pass


def main():
    settings = load_settings()
    scripts = find_scripts()
    variables = {"SHOPLITE_APP_PASSWORD": settings["SHOPLITE_APP_PASSWORD"]}

    print(f"Applying {len(scripts)} scripts to {HOST},{SQL_PORT} as sa.")
    print("WARNING: 001 drops and recreates SalesDB.\n")

    conn = connect(settings["SA_PASSWORD"])
    cursor = conn.cursor()

    for script in scripts:
        sql = substitute(script.read_text(encoding="utf-8"), variables, script.name)
        batches = split_batches(sql)
        print(f"-> {script.name} ({len(batches)} batches)")

        for number, (start_line, batch) in enumerate(batches, start=1):
            try:
                run_batch(cursor, batch)
            except pyodbc.Error as error:
                # The batch text isn't printed: in 004 it contains the real
                # shoplite_app password. The line number is enough to find it.
                print(f"\n[FAIL] {script.name}, batch {number} (starts at line {start_line}):")
                print(f"       {error.args[-1] if error.args else error}")
                print("       Stopped. Earlier batches were already committed; fix the")
                print("       script and run apply_schema.py again (001 starts from scratch).")
                conn.close()
                return 1

    conn.close()
    print("\nSalesDB schema applied successfully.")
    print("Next: python shoplite/db/seed.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
