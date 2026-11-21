/*
    005_report_procedures.sql
    Stored procedures behind the admin reports:
      dbo.usp_report_sales         GET /api/admin/reports/sales
      dbo.usp_report_top_products  GET /api/admin/reports/top-products

    These are deliberately heavy: they aggregate order history inside the
    same database that takes live orders. Reporting on a transactional
    database is a classic source of contention, and a realistic one.

    Shared rules:
      - A "sale" is an order with status paid, shipped, or delivered.
        pending_payment, payment_failed, and cancelled are excluded.
      - Revenue is item revenue (quantity * unit_price at time of order),
        without shipping, so every grouping adds up to the same total.
      - @start_date and @end_date are inclusive calendar dates in UTC
        (timestamps are stored in UTC; see 002_tables.sql).
      - Invalid input raises an error with THROW (error numbers 50001+),
        which the API turns into HTTP 400.

    Stored procedures remember the ANSI_NULLS and QUOTED_IDENTIFIER settings
    in effect when they're created (see the note in 003_indexes.sql), so set
    them explicitly first.
*/

USE SalesDB;
GO

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

/*
    Sales grouped by day, product, or state over a date range.

    Example: EXEC dbo.usp_report_sales @start_date = '2026-01-01',
                                       @end_date   = '2026-03-31',
                                       @group_by   = 'state';

    Each grouping is its own query (chosen with IF) rather than one query
    with CASE in the GROUP BY. Separate queries are easier to read, and each
    gets an execution plan suited to its shape.

    Deliberately no OPTION (RECOMPILE): SQL Server compiles each query's plan
    for the parameter values of the first call and reuses it. A plan built
    for a one-week range can be a poor fit for a two-year range ("parameter
    sniffing"). That's how most production procedures behave, and it's a
    realistic problem for Atlas to diagnose later, with Query Store evidence.
*/
CREATE OR ALTER PROCEDURE dbo.usp_report_sales
    @start_date DATE,
    @end_date   DATE,
    @group_by   VARCHAR(10) = 'day'   -- 'day', 'product', or 'state'
AS
BEGIN
    -- Don't send "N rows affected" messages; the API only needs the result set.
    SET NOCOUNT ON;

    IF @start_date IS NULL OR @end_date IS NULL OR @end_date < @start_date
        THROW 50001, 'Invalid date range: end_date must be on or after start_date.', 1;

    IF @group_by NOT IN ('day', 'product', 'state')
        THROW 50002, 'Invalid group_by: use day, product, or state.', 1;

    /*
        Every query below filters with a half-open range on the raw column:
            o.created_at >= @start_date AND o.created_at < DATEADD(DAY, 1, @end_date)
        - Comparing created_at directly (instead of CAST(created_at AS DATE)
          BETWEEN ...) keeps the filter "sargable": it can seek on
          IX_orders_created_at. The DATE parameter is converted to the
          column's type, never the column to the parameter's.
        - "< day after end" includes the whole end date, whatever the time.
        - The parameters are used directly, not copied into local variables.
          SQL Server can see (sniff) parameter values when compiling, but not
          local variable values, which get a generic guess instead.
    */

    IF @group_by = 'day'
    BEGIN
        SELECT
            CAST(o.created_at AS DATE)          AS sales_date,
            COUNT(DISTINCT o.order_id)          AS order_count,
            SUM(oi.quantity)                    AS units_sold,
            SUM(oi.quantity * oi.unit_price)    AS revenue
        FROM dbo.orders AS o
        JOIN dbo.order_items AS oi ON oi.order_id = o.order_id
        WHERE o.created_at >= @start_date
          AND o.created_at <  DATEADD(DAY, 1, @end_date)
          AND o.status IN ('paid', 'shipped', 'delivered')
        GROUP BY CAST(o.created_at AS DATE)
        ORDER BY sales_date;
    END
    ELSE IF @group_by = 'product'
    BEGIN
        SELECT
            p.product_id,
            p.sku,
            p.name                              AS product_name,
            COUNT(DISTINCT o.order_id)          AS order_count,
            SUM(oi.quantity)                    AS units_sold,
            SUM(oi.quantity * oi.unit_price)    AS revenue
        FROM dbo.orders AS o
        JOIN dbo.order_items AS oi ON oi.order_id = o.order_id
        JOIN dbo.products AS p     ON p.product_id = oi.product_id
        WHERE o.created_at >= @start_date
          AND o.created_at <  DATEADD(DAY, 1, @end_date)
          AND o.status IN ('paid', 'shipped', 'delivered')
        GROUP BY p.product_id, p.sku, p.name
        ORDER BY revenue DESC;
    END
    ELSE  -- 'state'
    BEGIN
        SELECT
            o.ship_state                        AS state,
            COUNT(DISTINCT o.order_id)          AS order_count,
            SUM(oi.quantity)                    AS units_sold,
            SUM(oi.quantity * oi.unit_price)    AS revenue
        FROM dbo.orders AS o
        JOIN dbo.order_items AS oi ON oi.order_id = o.order_id
        WHERE o.created_at >= @start_date
          AND o.created_at <  DATEADD(DAY, 1, @end_date)
          AND o.status IN ('paid', 'shipped', 'delivered')
        GROUP BY o.ship_state
        ORDER BY revenue DESC;
    END;
END;
GO

/*
    Best-selling products by units over a date range.

    Example: EXEC dbo.usp_report_top_products @start_date = '2026-01-01',
                                              @end_date   = '2026-03-31',
                                              @top        = 10;
*/
CREATE OR ALTER PROCEDURE dbo.usp_report_top_products
    @start_date DATE,
    @end_date   DATE,
    @top        INT = 10
AS
BEGIN
    SET NOCOUNT ON;

    IF @start_date IS NULL OR @end_date IS NULL OR @end_date < @start_date
        THROW 50001, 'Invalid date range: end_date must be on or after start_date.', 1;

    -- Cap the result size so a typo can't ask for millions of rows.
    IF @top IS NULL OR @top < 1 OR @top > 100
        THROW 50003, 'Invalid top: use a number from 1 to 100.', 1;

    -- Same half-open, sargable date filter as usp_report_sales (see the note there).
    SELECT TOP (@top)
        p.product_id,
        p.sku,
        p.name                              AS product_name,
        c.name                              AS category_name,
        SUM(oi.quantity)                    AS units_sold,
        SUM(oi.quantity * oi.unit_price)    AS revenue,
        COUNT(DISTINCT o.order_id)          AS order_count
    FROM dbo.orders AS o
    JOIN dbo.order_items AS oi ON oi.order_id = o.order_id
    JOIN dbo.products AS p     ON p.product_id = oi.product_id
    JOIN dbo.categories AS c   ON c.category_id = p.category_id
    WHERE o.created_at >= @start_date
      AND o.created_at <  DATEADD(DAY, 1, @end_date)
      AND o.status IN ('paid', 'shipped', 'delivered')
    GROUP BY p.product_id, p.sku, p.name, c.name
    ORDER BY units_sold DESC, revenue DESC;   -- revenue breaks ties
END;
GO
