# server/etl

ShopLite's daily **vendor ETL**: it reads the vendor's supplier catalog, prices, and stock (Postgres,
read-only) and loads them into SalesDB's `vendor.supplier_prices`.

**extract → validate → stage → merge**, every run recorded in `ops.etl_runs`.

| Where | Runs |
|---|---|
| prod-sql-01 | a SQL Agent job, weekdays about 14:00 UTC, after the vendor's 12:00 update (job: Phase 4 B5) |
| Your Mac | by hand, against the local Docker databases |

## Files

| File | What it is |
|---|---|
| `sql/001_ops_and_vendor_changes.sql` | `ops.etl_runs`, the new `supplier_prices` columns and index, staging, the clear and merge procedures |
| `sql/002_etl_login.sql` | The `shoplite_etl` login: insert staging, run the two procedures, read and write `ops.etl_runs`. Nothing else. |
| `sql/900_cutover_placeholders.sql` | **One time only:** removes Phase 1's placeholder supplier rows before the first run |
| `contract.py` | Checks the vendor against `knowledge/contracts/vendor-supplier-feed.yml` |
| `run_etl.py` | The ETL itself |
| `requirements.txt` | Its packages, installed in its own `.venv` |
| `.env.example` | Settings template (copy to `.env`) |
| `tests/test_etl.py` | Tests against the local Docker databases |

The SQL scripts are **not** part of `shoplite/db/apply_schema.py`, which drops and recreates SalesDB.
After a local `apply_schema.py` rebuild, run all three again before the next ETL run: 001 and 002
because the rebuild removed them, and 900 because `seed.py` put the placeholders back (the rebuild also
emptied `ops.etl_runs`, so 900 allows it).

## How a run works

1. **SalesDB first:** connect as `shoplite_etl`, take a lock (only one run at a time), insert a
   `running` row in `ops.etl_runs`.
2. **connect** to the vendor as `shoplite_reader` (TLS required unless it's localhost, 15 s timeout).
3. **validate** against the contract: columns and types, row counts, keys, SKU format, cost, currency,
   every item has a price and stock row, newest price change within 36 hours. Any problem stops the
   run **before** SalesDB changes.
4. **extract** about 10,000 joined rows, in the same read-only snapshot validation saw.
5. **stage** them into `vendor.supplier_prices_staging` (all or nothing).
6. **merge** in one short transaction: insert new, update changed, flag the ones that vanished as
   discontinued (stock 0, never deleted). Refuses if staging has fewer rows than the contract minimum.
7. The run row becomes `succeeded` with its counts, or `failed` with the stage and the message.

Running it twice in a row is safe: the second run counts everything as unchanged.

### Exit codes

| Code | Meaning | Recorded in `ops.etl_runs`? |
|---|---|---|
| 0 | Succeeded | yes |
| 2 | Configuration problem (missing setting, wrong login in the URL, no TLS, bad contract file) | no: nothing connected yet |
| 3 | SalesDB unreachable, login failed, or 001/002 not applied | no: nowhere to record it |
| 4 | Another run holds the lock | no: the other run is recording |
| 11 | Vendor unreachable (`connect`) | yes |
| 12 | Contract violated (`validate`) | yes, with every problem |
| 13 | Extract failed (`extract`) | yes |
| 14 | Staging failed (`stage`) | yes |
| 15 | Merge failed or refused (`merge`) | yes |

A row stuck in `running` with no `finished_at` means the process died mid-run (or SalesDB went away
before the failure could be written).

---

## Passwords

One new password for `shoplite_etl` (SQL Server's policy: 8+ characters, three of upper, lower,
digits, symbols). It goes to the `002` script (once, at run time) and to settings: **locally the root
`.env`'s `SHOPLITE_ETL_PASSWORD`** (no ETL `.env` needed: the ETL also builds its vendor URL from the
root's `SHOPLITE_READER_PASSWORD`, `VENDOR_HOST`, `VENDOR_PORT`, `POSTGRES_DB`, encoded); **on the VM**
`server\etl\.env`'s `SALESDB_PASSWORD` and the Neon `VENDOR_READER_URL`.

```bash
read -rs SHOPLITE_ETL_PASSWORD && export SHOPLITE_ETL_PASSWORD     # type, press Enter
```

---

## Local (Part C)

Needs: Docker running, vendor-pg set up and seeded (`vendor/README.md`), SalesDB built and seeded.

**1. Virtual environment and settings**

```bash
python3 -m venv server/etl/.venv
server/etl/.venv/bin/pip install -r server/etl/requirements.txt "pytest>=8.3,<9"
# Settings: nothing to copy. The root .env needs SHOPLITE_ETL_PASSWORD and SHOPLITE_READER_PASSWORD;
# everything else has a local default. (A server/etl/.env is only for overriding one value.)
```

(`pytest` only on your Mac; the VM doesn't need it.)

**2. SQL scripts**, run with `sqlcmd` inside the SQL Server container (it already knows the `sa`
password from its own settings). `-b` makes sqlcmd exit non-zero on any error:

```bash
SQLCMD='/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -I -b'

docker exec -i shoplite-sql sh -c "$SQLCMD" < server/etl/sql/001_ops_and_vendor_changes.sql

docker exec -i -e EP="$SHOPLITE_ETL_PASSWORD" shoplite-sql \
  sh -c "$SQLCMD -v SHOPLITE_ETL_PASSWORD=\"\$EP\"" < server/etl/sql/002_etl_login.sql

docker exec -i shoplite-sql sh -c "$SQLCMD -v CONFIRM=REPLACE_PLACEHOLDERS" \
  < server/etl/sql/900_cutover_placeholders.sql
```

The last one prints how many placeholder rows it removed. Run again, it still works until the first
ETL run succeeds; after that it refuses.

**3. Check the vendor against the contract** (SalesDB untouched):

```bash
server/etl/.venv/bin/python server/etl/run_etl.py --check
```

`Contract 1.0.0 OK (...)` with the row counts. If it says prices are too old, run
`python vendor/activity.py` once (the real feed would be stale too).

**4. First real run, then a second one:**

```bash
server/etl/.venv/bin/python server/etl/run_etl.py
server/etl/.venv/bin/python server/etl/run_etl.py
echo "exit code: $?"
```

The first inserts about 10,000 rows; the second inserts, updates, and discontinues 0. Exit code 0.

**5. Tests:**

```bash
server/etl/.venv/bin/python -m pytest server/etl/tests -v
```

They refuse anything that isn't localhost. Contract and merge tests change nothing (everything happens
in transactions that are rolled back). The `end_to_end` test really runs the ETL twice; skip it with
`-k "not end_to_end"`.

**6. See a failure on purpose:** stop the vendor container (`docker stop vendor-pg`), run the ETL, and
look at the exit code (11) and the run row (below). Start it again with `docker start vendor-pg`.

---

## On prod-sql-01 (Part C, in the Phase 4 maintenance window)

In PowerShell as Administrator, from `C:\atlas` after pulling the latest code. ODBC Driver 18 and
Python are already there from Phase 2.

**1. Virtual environment:**

```powershell
cd C:\atlas
py -3.12 -m venv server\etl\.venv
server\etl\.venv\Scripts\pip install -r server\etl\requirements.txt
```

**2. Settings file**, with the production values: the Neon direct host with `?sslmode=require`, and
`SALESDB_HOST=localhost`:

```powershell
Copy-Item server\etl\.env.example server\etl\.env
notepad server\etl\.env
```

**Already have this file from before (it predates layered settings)?** Add the line `LOAD_ROOT_ENV=false` to it, so the root `.env` is never read even when you run this by hand as Administrator.

Lock it down like the root `.env`: only administrators, SYSTEM, and the **SQL Agent service account**
(the job runs the ETL as that account) can read it:

```powershell
icacls C:\atlas\server\etl\.env /inheritance:r /grant:r "Administrators:F" "SYSTEM:F" "NT SERVICE\SQLSERVERAGENT:R"
```

The rest of `server\etl`, the `.venv`, and the contract file hold no secrets; they keep the normal
read access inherited from `C:\atlas`, which is all the job needs.

**3. SQL scripts**, as the SQL admin with Windows authentication. The ETL password is asked for, not
typed on the command line in plain sight or kept in history:

```powershell
sqlcmd -S localhost -E -I -b -i server\etl\sql\001_ops_and_vendor_changes.sql

$secure = Read-Host "shoplite_etl password" -AsSecureString
$plain = [System.Net.NetworkCredential]::new('', $secure).Password
sqlcmd -S localhost -E -I -b -v SHOPLITE_ETL_PASSWORD="$plain" -i server\etl\sql\002_etl_login.sql
Remove-Variable plain, secure

sqlcmd -S localhost -E -I -b -v CONFIRM=REPLACE_PLACEHOLDERS -i server\etl\sql\900_cutover_placeholders.sql
```

**4. Check, then the first run by hand:**

```powershell
server\etl\.venv\Scripts\python server\etl\run_etl.py --check
server\etl\.venv\Scripts\python server\etl\run_etl.py
$LASTEXITCODE
```

The scheduled job comes in B5.

---

## Reading `ops.etl_runs`

```sql
SELECT TOP 10 run_id, started_at, DATEDIFF(SECOND, started_at, finished_at) AS seconds, status,
       rows_read, rows_inserted, rows_updated, rows_discontinued, rows_unchanged, rows_skipped,
       vendor_newest_price_at, error_stage, LEFT(error_message, 200) AS error
FROM ops.etl_runs
ORDER BY run_id DESC;
```

What a normal daily run looks like (one vendor day after the previous run): `succeeded`, a few
seconds, about 3,000 updated (prices drift on about 8% of items and stock moves on about 25%), about 20
discontinued, about 10 inserted, `rows_skipped` 0. A second run the same day: everything unchanged.

| You see | Likely cause |
|---|---|
| `failed`, `validate`, "hours old" | The vendor-day workflow didn't run (or `VENDOR_ACTIVITY_ENABLED=false`) |
| `failed`, `validate`, "column missing" or "type expected" | The vendor changed their schema: the contract and ETL must change together |
| `failed`, `connect` | Neon down, wrong host or password in `.env`, or no outbound internet from the VM |
| `failed`, `merge`, "Refusing to merge" | The feed was much smaller than the contract minimum |
| `running`, no `finished_at`, hours old | The process was killed or the VM was deallocated mid-run |
| No row at all today | The job didn't run: check SQL Agent job history (exit code 2, 3, or 4 leaves no row) |

## Check the ETL login

As `shoplite_etl` (the tests do this too), `SELECT TOP 1 * FROM dbo.orders` must fail with "The SELECT
permission was denied", and so must reading `vendor.supplier_prices` directly.
