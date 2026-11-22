# server/reporting

**ReportingDB**: daily sales summaries rebuilt from SalesDB, on the same SQL Server. It feeds Grafana
and forecasting in later phases. The storefront's admin reports keep querying SalesDB directly for now,
on purpose: reporting load on the transactional database is a realistic source of contention for Atlas
to diagnose.

| Table (schema `rpt`) | One row per |
|---|---|
| `daily_sales` | day: orders, units, revenue |
| `daily_sales_by_product` | day and product (with SKU, name, category) |
| `daily_sales_by_state` | day and ship state |
| `daily_sales_by_category` | day and category |

Same rules as the admin reports (`shoplite/db/005_report_procedures.sql`): a **sale** is an order that's
paid, shipped, or delivered; **revenue** is item revenue (quantity × price at order time), without
shipping; dates are **UTC**.

## Files

| File | What it is |
|---|---|
| `sql/001_database.sql` | Creates ReportingDB **only if missing** (never drops): SIMPLE recovery, Query Store, owner `sa` |
| `sql/002_tables_and_role.sql` | The four `rpt` tables, `ops.refresh_runs`, and the `reporting_reader` role |
| `sql/003_refresh_procedures.sql` | `rpt.usp_refresh_sales`: incremental or full refresh |
| `queries/verify.sql` | Freshness and "matches SalesDB" checks: every Part 2 row should say PASS |

All three scripts are safe to re-run and safe in production.

## The refresh

```sql
EXEC ReportingDB.rpt.usp_refresh_sales;                    -- incremental: the daily run
EXEC ReportingDB.rpt.usp_refresh_sales @days = 7;          -- incremental, wider window
EXEC ReportingDB.rpt.usp_refresh_sales @mode = 'full';     -- rebuild all history
```

- **Incremental** rebuilds the last 3 days (today included) and reaches back to the day of the last
  successful refresh if that's older, so Monday's run picks up Friday's late orders (the VM is off at
  weekends) and a failed day is caught up the next time. The very first refresh is always full.
- **Full** empties the tables and rebuilds from the first order. It's also how changes older than the
  window get in, such as an order cancelled two weeks after it was paid. The job (B5) runs incremental
  daily and full weekly.
- **How:** one cutoff time, one read of SalesDB into a temporary table, then one short transaction that
  replaces the affected days in all four tables. The four tables always agree with each other, and
  readers never see a half-done refresh.
- **Logged** in `ops.refresh_runs` (`running` → `succeeded` or `failed` with the message). A failure is
  re-raised, so the SQL Agent job shows it, and the tables keep their previous numbers.

It runs as the SQL admin (or the job owner, `sa`): it reads SalesDB directly and `TRUNCATE` needs more
than `EXECUTE`.

## `reporting_reader`

A role with `SELECT` on the `rpt` schema and on `ops.refresh_runs`, nothing else: no writes, no
`EXECUTE`, no access to SalesDB. **No login is in it yet.** Grafana and Atlas get their own logins in
later phases, added with `ALTER ROLE reporting_reader ADD MEMBER ...`.

---

## Local (Part C1, step 4)

Needs: SalesDB built and seeded. The scripts run with `sqlcmd` inside the SQL Server container (it
knows the `sa` password from its own settings); `-b` stops at the first error:

```bash
SQLCMD='/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -I -b'

docker exec -i shoplite-sql sh -c "$SQLCMD" < server/reporting/sql/001_database.sql
docker exec -i shoplite-sql sh -c "$SQLCMD" < server/reporting/sql/002_tables_and_role.sql
docker exec -i shoplite-sql sh -c "$SQLCMD" < server/reporting/sql/003_refresh_procedures.sql
```

**First refresh** (full, because there's no earlier one), then an incremental one:

```bash
docker exec -i shoplite-sql sh -c "$SQLCMD -d ReportingDB -Q \"EXEC rpt.usp_refresh_sales;\""
docker exec -i shoplite-sql sh -c "$SQLCMD -d ReportingDB -Q \"EXEC rpt.usp_refresh_sales;\""
```

The first prints `mode full` with all history (two years of seeded orders); the second `mode
incremental`, 3 days, in about a second.

**Compare with SalesDB:** open `server/reporting/queries/verify.sql` in VS Code's SQL Server extension
(connected as `sa`) and run it. Every Part 2 row should say PASS, and 2.2 should return no rows. (Or
in the terminal, narrower output: `docker exec -i shoplite-sql sh -c "$SQLCMD -W -s '|'" <
server/reporting/queries/verify.sql`.)

**Idempotent:** run the incremental refresh again and re-run `verify.sql`: same numbers, still PASS.

**After a local SalesDB rebuild** (`apply_schema.py` and `seed.py`), ReportingDB still holds the old
database's numbers. Run `EXEC rpt.usp_refresh_sales @mode = 'full';` to rebuild it from the new data.

---

## On prod-sql-01 (Part C3, in the Phase 4 maintenance window)

In PowerShell as Administrator, from `C:\atlas`, with Windows authentication:

```powershell
sqlcmd -S localhost -E -I -b -i server\reporting\sql\001_database.sql
sqlcmd -S localhost -E -I -b -i server\reporting\sql\002_tables_and_role.sql
sqlcmd -S localhost -E -I -b -i server\reporting\sql\003_refresh_procedures.sql

sqlcmd -S localhost -E -d ReportingDB -b -Q "EXEC rpt.usp_refresh_sales;"
sqlcmd -S localhost -E -I -W -s "|" -i server\reporting\queries\verify.sql
```

The first refresh is full: on the B2as_v2 it reads all order history once, so do it with
`TRAFFIC_ENABLED=false` (the maintenance window), and watch CPU credits. Daily incremental runs are
small. The scheduled job comes in B5; ReportingDB's weekly full backup in B4/B5.

With traffic running, a 2.1/2.2 FAIL by a handful of orders right after a refresh can be real orders
changing status around the cutoff; a full refresh with traffic off must PASS.

---

## Reading `ops.refresh_runs`

```sql
SELECT TOP 10 run_id, mode, from_date, cutoff_at, status,
       DATEDIFF(SECOND, started_at, finished_at) AS seconds,
       sale_lines, days_rebuilt, rows_written, LEFT(error_message, 200) AS error
FROM ReportingDB.ops.refresh_runs
ORDER BY run_id DESC;
```

| You see | Likely cause |
|---|---|
| `failed`, "Invalid object name 'SalesDB.dbo...'" | SalesDB missing or renamed (a restore under another name?) |
| `failed`, a lock timeout or deadlock message | Contention with live orders or the ETL at refresh time |
| `running`, no `finished_at` | The session was killed or the VM was deallocated mid-refresh; the transaction rolled back, tables unchanged |
| `days_rebuilt` larger than usual | It caught up a gap: a weekend, or days without a successful refresh |
| `seconds` much higher than usual | More history in the window, a changed plan (Query Store), or SalesDB busy |
| No row today | The job didn't run: check SQL Agent job history |
