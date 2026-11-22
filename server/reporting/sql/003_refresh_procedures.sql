/*
    server/reporting/sql/003_refresh_procedures.sql
    rpt.usp_refresh_sales: rebuilds ReportingDB's report tables from SalesDB.
    Run after 002, as the SQL admin. Safe to re-run (CREATE OR ALTER).

        EXEC rpt.usp_refresh_sales;                          -- incremental, the usual daily run
        EXEC rpt.usp_refresh_sales @days = 7;                -- incremental, a wider window
        EXEC rpt.usp_refresh_sales @mode = 'full';           -- rebuild everything

    Incremental: rebuilds the last @days days (today included), and reaches
    further back to the day of the last successful refresh if that's older.
    So the weekend gap (VM off) and a failed day are caught up
    automatically: Monday's run rebuilds from Friday, including Friday's
    orders after Friday's refresh. If there has never been a successful
    refresh, it does a full one.

    Full: empties the report tables and rebuilds all history. Also the fix
    for changes older than the window, e.g. an order cancelled a week after
    it was paid (the job runs a full refresh weekly; see server/jobs/).

    How it works (each step is why the totals can be trusted):
      1. One cutoff time is fixed at the start: only orders created before
         it are included, in every table.
      2. SalesDB is read ONCE, into a temporary table of sale lines. All four
         tables are built from that same set of lines, so they always agree
         with each other, and SalesDB (busy with live orders) is touched by
         one query instead of four.
      3. One short transaction replaces the affected days in all four tables.
         A reader sees the old numbers or the new ones, never half of each.
      4. Every run is logged in ops.refresh_runs; a failure is logged with
         its message and re-raised, so the SQL Agent job fails visibly.

    Replacing the window (delete the days, insert them again) is what makes
    it idempotent: running it twice gives the same rows. Simpler than the
    ETL's MERGE because these rows are derived, never edited.

    Runs as the SQL admin or the job owner (sa): TRUNCATE (full mode) needs
    ALTER permission, and it reads SalesDB directly by three-part name.
*/

USE ReportingDB;
GO
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

CREATE OR ALTER PROCEDURE rpt.usp_refresh_sales
    @mode VARCHAR(20) = 'incremental',      -- incremental or full
    @days INT = 3                           -- incremental only: minimum days to rebuild, today included
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;                      -- any error rolls back the whole transaction

    IF @mode IS NULL OR @mode NOT IN ('incremental', 'full')
        THROW 50201, N'@mode must be incremental or full.', 1;
    IF @mode = 'incremental' AND (@days IS NULL OR @days NOT BETWEEN 1 AND 60)
        THROW 50202, N'@days must be between 1 and 60.', 1;

    DECLARE @cutoff DATETIME2(3) = SYSUTCDATETIME();
    DECLARE @today  DATE = CAST(@cutoff AS DATE);
    DECLARE @from   DATE;

    -- The day of the last successful refresh's cutoff: everything before
    -- that day was complete when it ran.
    DECLARE @last_good DATE = (SELECT CAST(MAX(cutoff_at) AS DATE)
                               FROM ops.refresh_runs WHERE status = 'succeeded');
    IF @mode = 'incremental' AND @last_good IS NULL
        SET @mode = 'full';                 -- nothing to build on yet

    IF @mode = 'full'
        -- From the first order ever; NULL if SalesDB has no orders.
        SET @from = (SELECT CAST(MIN(created_at) AS DATE) FROM SalesDB.dbo.orders);
    ELSE
    BEGIN
        SET @from = DATEADD(DAY, 1 - @days, @today);
        IF @last_good < @from
            SET @from = @last_good;         -- catch up a gap
    END;

    INSERT INTO ops.refresh_runs (mode, from_date, cutoff_at) VALUES (@mode, @from, @cutoff);
    DECLARE @run_id INT = SCOPE_IDENTITY();

    DECLARE @lines INT = 0, @rows INT = 0;
    DECLARE @days_rebuilt INT = CASE WHEN @from IS NULL THEN 0 ELSE DATEDIFF(DAY, @from, @today) + 1 END;

    BEGIN TRY
        -- 1-2. Read SalesDB once. Product and category names are taken now,
        --      so the report rows carry them.
        CREATE TABLE #sale_lines
        (
            order_id       INT           NOT NULL,
            sales_date     DATE          NOT NULL,
            state          CHAR(2)       NOT NULL,
            product_id     INT           NOT NULL,
            sku            VARCHAR(20)   NOT NULL,
            product_name   NVARCHAR(200) NOT NULL,
            category_id    INT           NOT NULL,
            category_name  NVARCHAR(100) NOT NULL,
            quantity       INT           NOT NULL,
            line_revenue   DECIMAL(14,2) NOT NULL
        );

        INSERT INTO #sale_lines
            (order_id, sales_date, state, product_id, sku, product_name, category_id, category_name,
             quantity, line_revenue)
        SELECT o.order_id, CAST(o.created_at AS DATE), o.ship_state,
               p.product_id, p.sku, p.name, c.category_id, c.name,
               oi.quantity, oi.quantity * oi.unit_price
        FROM SalesDB.dbo.orders      AS o
        JOIN SalesDB.dbo.order_items AS oi ON oi.order_id = o.order_id
        JOIN SalesDB.dbo.products    AS p  ON p.product_id = oi.product_id
        JOIN SalesDB.dbo.categories  AS c  ON c.category_id = p.category_id
        WHERE o.created_at >= @from          -- @from NULL (no orders): no rows
          AND o.created_at <  @cutoff
          AND o.status IN ('paid', 'shipped', 'delivered');
        SET @lines = @@ROWCOUNT;

        DECLARE @refreshed_at DATETIME2(3) = SYSUTCDATETIME();

        -- 3. Replace the days, all four tables in one transaction.
        BEGIN TRANSACTION;

        IF @mode = 'full'
        BEGIN
            -- TRUNCATE: removes all rows at once with minimal logging,
            -- instead of logging each deleted row.
            TRUNCATE TABLE rpt.daily_sales;
            TRUNCATE TABLE rpt.daily_sales_by_product;
            TRUNCATE TABLE rpt.daily_sales_by_state;
            TRUNCATE TABLE rpt.daily_sales_by_category;
        END
        ELSE
        BEGIN
            DELETE FROM rpt.daily_sales             WHERE sales_date >= @from;
            DELETE FROM rpt.daily_sales_by_product  WHERE sales_date >= @from;
            DELETE FROM rpt.daily_sales_by_state    WHERE sales_date >= @from;
            DELETE FROM rpt.daily_sales_by_category WHERE sales_date >= @from;
        END;

        -- COUNT(DISTINCT order_id): an order with three lines is one order.
        INSERT INTO rpt.daily_sales (sales_date, order_count, units_sold, revenue, refreshed_at)
        SELECT sales_date, COUNT(DISTINCT order_id), SUM(quantity), SUM(line_revenue), @refreshed_at
        FROM #sale_lines
        GROUP BY sales_date;
        SET @rows += @@ROWCOUNT;

        INSERT INTO rpt.daily_sales_by_product
            (sales_date, product_id, sku, product_name, category_name, order_count, units_sold, revenue, refreshed_at)
        SELECT sales_date, product_id, sku, product_name, category_name,
               COUNT(DISTINCT order_id), SUM(quantity), SUM(line_revenue), @refreshed_at
        FROM #sale_lines
        GROUP BY sales_date, product_id, sku, product_name, category_name;
        SET @rows += @@ROWCOUNT;

        INSERT INTO rpt.daily_sales_by_state (sales_date, state, order_count, units_sold, revenue, refreshed_at)
        SELECT sales_date, state, COUNT(DISTINCT order_id), SUM(quantity), SUM(line_revenue), @refreshed_at
        FROM #sale_lines
        GROUP BY sales_date, state;
        SET @rows += @@ROWCOUNT;

        INSERT INTO rpt.daily_sales_by_category
            (sales_date, category_id, category_name, order_count, units_sold, revenue, refreshed_at)
        SELECT sales_date, category_id, category_name,
               COUNT(DISTINCT order_id), SUM(quantity), SUM(line_revenue), @refreshed_at
        FROM #sale_lines
        GROUP BY sales_date, category_id, category_name;
        SET @rows += @@ROWCOUNT;

        COMMIT TRANSACTION;

        -- 4. Log the success.
        UPDATE ops.refresh_runs
        SET finished_at = SYSUTCDATETIME(), status = 'succeeded',
            sale_lines = @lines, days_rebuilt = @days_rebuilt, rows_written = @rows
        WHERE run_id = @run_id;

        SELECT @run_id AS run_id, @mode AS mode, @from AS from_date, @cutoff AS cutoff_at,
               @lines AS sale_lines, @days_rebuilt AS days_rebuilt, @rows AS rows_written;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;           -- the report tables keep their previous numbers

        UPDATE ops.refresh_runs
        SET finished_at = SYSUTCDATETIME(), status = 'failed',
            sale_lines = @lines, error_message = LEFT(ERROR_MESSAGE(), 2000)
        WHERE run_id = @run_id;

        THROW;                              -- re-raise: the caller (the job) must see the failure
    END CATCH;
END;
GO
