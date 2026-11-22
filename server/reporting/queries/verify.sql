/*
    server/reporting/queries/verify.sql
    Checks that ReportingDB is fresh and matches SalesDB. Read-only; run as
    often as you like, after a refresh.

    How to run: in VS Code's SQL Server extension, connect as the SQL admin,
    open this file, and run it (or select one query). Or with sqlcmd; see
    server/reporting/README.md.

    Part 1 (overview):  the latest refreshes and what the tables hold.
    Part 2 (checks):    every row should say PASS.

    Comparisons with SalesDB count only orders created before the last
    successful refresh's cutoff, with the same rules as the refresh (paid,
    shipped, delivered; item revenue), so a refresh that's simply older than
    the newest orders still matches.

    Why a check can say FAIL while nothing is broken: an order's status
    changed after the refresh (a pending payment became paid around the
    cutoff, or an old order was cancelled, outside the incremental window).
    Run EXEC rpt.usp_refresh_sales @mode = 'full'; and check again: with
    traffic off, it must then PASS.

    Each query stands alone (no variables shared across GO), so any one can
    be run on its own.
*/

USE ReportingDB;
GO

-- ===========================================================================
-- Part 1: Overview
-- ===========================================================================

-- 1.1 The latest refreshes. Expect "succeeded"; a daily incremental run
--     takes seconds, a full one longer. days_rebuilt above 3 means it
--     caught up a gap (a weekend or a failed day).
SELECT TOP 10 run_id, mode, from_date, cutoff_at, status,
       DATEDIFF(SECOND, started_at, finished_at) AS seconds,
       sale_lines, days_rebuilt, rows_written, LEFT(error_message, 200) AS error
FROM ops.refresh_runs
ORDER BY run_id DESC;
GO

-- 1.2 What each table holds: rows, the date range, when it was last refreshed.
SELECT 'rpt.daily_sales' AS table_name, COUNT(*) AS row_count, MIN(sales_date) AS first_day,
       MAX(sales_date) AS last_day, MAX(refreshed_at) AS last_refreshed FROM rpt.daily_sales
UNION ALL SELECT 'rpt.daily_sales_by_product', COUNT(*), MIN(sales_date), MAX(sales_date), MAX(refreshed_at)
          FROM rpt.daily_sales_by_product
UNION ALL SELECT 'rpt.daily_sales_by_state', COUNT(*), MIN(sales_date), MAX(sales_date), MAX(refreshed_at)
          FROM rpt.daily_sales_by_state
UNION ALL SELECT 'rpt.daily_sales_by_category', COUNT(*), MIN(sales_date), MAX(sales_date), MAX(refreshed_at)
          FROM rpt.daily_sales_by_category;
GO

-- 1.3 The last 7 days side by side: ReportingDB and SalesDB.
WITH last_run AS (
    SELECT TOP 1 cutoff_at FROM ops.refresh_runs WHERE status = 'succeeded' ORDER BY run_id DESC
),
source AS (
    SELECT CAST(o.created_at AS DATE) AS sales_date, COUNT(DISTINCT o.order_id) AS order_count,
           SUM(oi.quantity) AS units_sold, SUM(oi.quantity * oi.unit_price) AS revenue
    FROM SalesDB.dbo.orders AS o
    JOIN SalesDB.dbo.order_items AS oi ON oi.order_id = o.order_id
    WHERE o.created_at < (SELECT cutoff_at FROM last_run)
      AND o.created_at >= DATEADD(DAY, -6, CAST((SELECT cutoff_at FROM last_run) AS DATE))
      AND o.status IN ('paid', 'shipped', 'delivered')
    GROUP BY CAST(o.created_at AS DATE)
)
SELECT COALESCE(r.sales_date, s.sales_date) AS sales_date,
       r.order_count AS report_orders,  s.order_count AS salesdb_orders,
       r.revenue     AS report_revenue, s.revenue     AS salesdb_revenue
FROM (SELECT * FROM rpt.daily_sales
      WHERE sales_date >= DATEADD(DAY, -6, CAST((SELECT cutoff_at FROM last_run) AS DATE))) AS r
FULL OUTER JOIN source AS s ON s.sales_date = r.sales_date
ORDER BY sales_date DESC;
GO

-- ===========================================================================
-- Part 2: Checks (every row should say PASS)
-- ===========================================================================

-- 2.1 Grand totals match SalesDB: orders, units, revenue, over all history.
--     SUM(order_count) over days counts each order once (an order has one date).
WITH last_run AS (
    SELECT TOP 1 cutoff_at FROM ops.refresh_runs WHERE status = 'succeeded' ORDER BY run_id DESC
),
report AS (
    SELECT SUM(order_count) AS orders, SUM(units_sold) AS units, SUM(revenue) AS revenue FROM rpt.daily_sales
),
source AS (
    SELECT COUNT(DISTINCT o.order_id) AS orders, SUM(oi.quantity) AS units,
           SUM(oi.quantity * oi.unit_price) AS revenue
    FROM SalesDB.dbo.orders AS o
    JOIN SalesDB.dbo.order_items AS oi ON oi.order_id = o.order_id
    WHERE o.created_at < (SELECT cutoff_at FROM last_run)
      AND o.status IN ('paid', 'shipped', 'delivered')
)
SELECT '2.1 grand totals match SalesDB' AS check_name,
       report.orders  AS report_orders,  source.orders  AS salesdb_orders,
       report.units   AS report_units,   source.units   AS salesdb_units,
       report.revenue AS report_revenue, source.revenue AS salesdb_revenue,
       CASE WHEN (SELECT cutoff_at FROM last_run) IS NULL THEN 'FAIL (no successful refresh yet)'
            WHEN report.orders = source.orders AND report.units = source.units
             AND report.revenue = source.revenue THEN 'PASS'
            ELSE 'FAIL (see 2.2 for the days that differ)' END AS result
FROM report CROSS JOIN source;
GO

-- 2.2 Every day matches SalesDB. Lists up to 20 days that differ; no rows
--     means every day matches (PASS).
WITH last_run AS (
    SELECT TOP 1 cutoff_at FROM ops.refresh_runs WHERE status = 'succeeded' ORDER BY run_id DESC
),
source AS (
    SELECT CAST(o.created_at AS DATE) AS sales_date, COUNT(DISTINCT o.order_id) AS order_count,
           SUM(oi.quantity) AS units_sold, SUM(oi.quantity * oi.unit_price) AS revenue
    FROM SalesDB.dbo.orders AS o
    JOIN SalesDB.dbo.order_items AS oi ON oi.order_id = o.order_id
    WHERE o.created_at < (SELECT cutoff_at FROM last_run)
      AND o.status IN ('paid', 'shipped', 'delivered')
    GROUP BY CAST(o.created_at AS DATE)
)
SELECT TOP 20 '2.2 day differs from SalesDB (FAIL)' AS check_name,
       COALESCE(r.sales_date, s.sales_date) AS sales_date,
       r.order_count AS report_orders,  s.order_count AS salesdb_orders,
       r.revenue     AS report_revenue, s.revenue     AS salesdb_revenue
FROM rpt.daily_sales AS r
FULL OUTER JOIN source AS s ON s.sales_date = r.sales_date
WHERE r.sales_date IS NULL OR s.sales_date IS NULL
   OR r.order_count <> s.order_count OR r.units_sold <> s.units_sold OR r.revenue <> s.revenue
ORDER BY sales_date DESC;
GO

-- 2.3 Each breakdown adds up to the daily totals, day by day. These come
--     from the same lines in the same transaction, so they must match
--     exactly, whatever happened in SalesDB since. Order counts are compared
--     only for states: an order has one ship state, but can contain several
--     products and categories.
WITH product AS (
    SELECT sales_date, SUM(units_sold) AS units, SUM(revenue) AS revenue
    FROM rpt.daily_sales_by_product GROUP BY sales_date
),
state AS (
    SELECT sales_date, SUM(order_count) AS orders, SUM(units_sold) AS units, SUM(revenue) AS revenue
    FROM rpt.daily_sales_by_state GROUP BY sales_date
),
category AS (
    SELECT sales_date, SUM(units_sold) AS units, SUM(revenue) AS revenue
    FROM rpt.daily_sales_by_category GROUP BY sales_date
)
SELECT '2.3 by product adds up' AS check_name,
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE CONCAT('FAIL (', COUNT(*), ' days)') END AS result
FROM rpt.daily_sales AS d FULL OUTER JOIN product AS b ON b.sales_date = d.sales_date
WHERE d.sales_date IS NULL OR b.sales_date IS NULL OR b.units <> d.units_sold OR b.revenue <> d.revenue
UNION ALL
SELECT '2.3 by state adds up',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE CONCAT('FAIL (', COUNT(*), ' days)') END
FROM rpt.daily_sales AS d FULL OUTER JOIN state AS b ON b.sales_date = d.sales_date
WHERE d.sales_date IS NULL OR b.sales_date IS NULL OR b.orders <> d.order_count
   OR b.units <> d.units_sold OR b.revenue <> d.revenue
UNION ALL
SELECT '2.3 by category adds up',
       CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE CONCAT('FAIL (', COUNT(*), ' days)') END
FROM rpt.daily_sales AS d FULL OUTER JOIN category AS b ON b.sales_date = d.sales_date
WHERE d.sales_date IS NULL OR b.sales_date IS NULL OR b.units <> d.units_sold OR b.revenue <> d.revenue;
GO

-- 2.4 Fresh: the last successful refresh is from today (UTC) and the
--     newest run didn't fail. On a weekday after the job's time, expect
--     PASS; before it (or on a weekend, VM off) the previous weekday's is normal.
SELECT '2.4 refreshed today' AS check_name,
       MAX(CASE WHEN status = 'succeeded' THEN cutoff_at END) AS last_success_cutoff,
       CASE WHEN CAST(MAX(CASE WHEN status = 'succeeded' THEN cutoff_at END) AS DATE)
                 = CAST(SYSUTCDATETIME() AS DATE)
             AND (SELECT TOP 1 status FROM ops.refresh_runs ORDER BY run_id DESC) = 'succeeded'
            THEN 'PASS' ELSE 'FAIL (not refreshed today, or the latest run failed: see 1.1)' END AS result
FROM ops.refresh_runs;
GO

-- 2.5 The read-only role has exactly SELECT, nothing more.
SELECT '2.5 reporting_reader is read-only' AS check_name,
       CASE WHEN EXISTS (SELECT 1 FROM sys.database_permissions AS p
                         JOIN sys.database_principals AS r ON r.principal_id = p.grantee_principal_id
                         WHERE r.name = N'reporting_reader' AND p.permission_name <> 'SELECT')
            THEN 'FAIL (has more than SELECT)'
            WHEN NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'reporting_reader')
            THEN 'FAIL (role missing: run 002)'
            ELSE 'PASS' END AS result;
GO
