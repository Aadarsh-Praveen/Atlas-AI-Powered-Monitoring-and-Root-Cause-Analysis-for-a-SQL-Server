/*
    server/reporting/sql/002_tables_and_role.sql
    ReportingDB's tables and its read-only role. Run after 001, as the SQL
    admin. Safe to re-run: everything is created only if missing, and data
    is kept.

    Schema rpt: the report tables. One row per day (and per product, state,
    or category). Rebuilt by rpt.usp_refresh_sales (003); never edited by hand.

    Schema ops: rpt.usp_refresh_sales logs every refresh in ops.refresh_runs,
    the same idea as SalesDB's ops.etl_runs. Atlas will read both.

    Same rules as the admin reports (shoplite/db/005_report_procedures.sql),
    so the two always agree:
      - A sale is an order with status paid, shipped, or delivered.
      - revenue is item revenue (quantity * unit_price at the time of the
        order), without shipping, so every breakdown adds up to the same total.
      - sales_date is the order's calendar date in UTC.
*/

USE ReportingDB;
GO
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF SCHEMA_ID(N'rpt') IS NULL EXEC (N'CREATE SCHEMA rpt AUTHORIZATION dbo;');
GO
IF SCHEMA_ID(N'ops') IS NULL EXEC (N'CREATE SCHEMA ops AUTHORIZATION dbo;');
GO

-- ---------------------------------------------------------------------------
-- Report tables. Names (sku, product_name, category_name) are copied in at
-- refresh time, so a reader never needs SalesDB to make sense of a row.
-- ---------------------------------------------------------------------------
IF OBJECT_ID(N'rpt.daily_sales', N'U') IS NULL
    CREATE TABLE rpt.daily_sales
    (
        sales_date    DATE          NOT NULL,
        order_count   INT           NOT NULL,
        units_sold    INT           NOT NULL,
        revenue       DECIMAL(14,2) NOT NULL,
        refreshed_at  DATETIME2(3)  NOT NULL,
        CONSTRAINT PK_daily_sales PRIMARY KEY (sales_date)
    );
GO

IF OBJECT_ID(N'rpt.daily_sales_by_product', N'U') IS NULL
BEGIN
    CREATE TABLE rpt.daily_sales_by_product
    (
        sales_date     DATE          NOT NULL,
        product_id     INT           NOT NULL,
        sku            VARCHAR(20)   NOT NULL,
        product_name   NVARCHAR(200) NOT NULL,
        category_name  NVARCHAR(100) NOT NULL,
        order_count    INT           NOT NULL,
        units_sold     INT           NOT NULL,
        revenue        DECIMAL(14,2) NOT NULL,
        refreshed_at   DATETIME2(3)  NOT NULL,
        CONSTRAINT PK_daily_sales_by_product PRIMARY KEY (sales_date, product_id)
    );
    -- One product's history over time (a product trend, later a forecast).
    CREATE INDEX IX_daily_sales_by_product_product_id
        ON rpt.daily_sales_by_product (product_id, sales_date)
        INCLUDE (units_sold, revenue);
END;
GO

IF OBJECT_ID(N'rpt.daily_sales_by_state', N'U') IS NULL
    CREATE TABLE rpt.daily_sales_by_state
    (
        sales_date    DATE          NOT NULL,
        state         CHAR(2)       NOT NULL,     -- the order's ship_state, as in the admin report
        order_count   INT           NOT NULL,
        units_sold    INT           NOT NULL,
        revenue       DECIMAL(14,2) NOT NULL,
        refreshed_at  DATETIME2(3)  NOT NULL,
        CONSTRAINT PK_daily_sales_by_state PRIMARY KEY (sales_date, state)
    );
GO

IF OBJECT_ID(N'rpt.daily_sales_by_category', N'U') IS NULL
    CREATE TABLE rpt.daily_sales_by_category
    (
        sales_date     DATE          NOT NULL,
        category_id    INT           NOT NULL,
        category_name  NVARCHAR(100) NOT NULL,
        order_count    INT           NOT NULL,
        units_sold     INT           NOT NULL,
        revenue        DECIMAL(14,2) NOT NULL,
        refreshed_at   DATETIME2(3)  NOT NULL,
        CONSTRAINT PK_daily_sales_by_category PRIMARY KEY (sales_date, category_id)
    );
GO

-- ---------------------------------------------------------------------------
-- The refresh log
-- ---------------------------------------------------------------------------
IF OBJECT_ID(N'ops.refresh_runs', N'U') IS NULL
BEGIN
    CREATE TABLE ops.refresh_runs
    (
        run_id         INT IDENTITY(1,1) NOT NULL,
        mode           VARCHAR(20)    NOT NULL,       -- incremental or full
        from_date      DATE           NULL,           -- first day rebuilt (NULL: no sales at all)
        cutoff_at      DATETIME2(3)   NULL,           -- orders created before this were included
        started_at     DATETIME2(3)   NOT NULL CONSTRAINT DF_refresh_runs_started_at DEFAULT (SYSUTCDATETIME()),
        finished_at    DATETIME2(3)   NULL,
        status         VARCHAR(20)    NOT NULL CONSTRAINT DF_refresh_runs_status DEFAULT ('running'),
        sale_lines     INT            NULL,           -- order lines read from SalesDB
        days_rebuilt   INT            NULL,
        rows_written   INT            NULL,           -- across the four report tables
        error_message  NVARCHAR(2000) NULL,

        CONSTRAINT PK_refresh_runs PRIMARY KEY (run_id),
        CONSTRAINT CK_refresh_runs_mode CHECK (mode IN ('incremental', 'full')),
        CONSTRAINT CK_refresh_runs_status CHECK (status IN ('running', 'succeeded', 'failed'))
    );
    CREATE INDEX IX_refresh_runs_started_at ON ops.refresh_runs (started_at DESC);
END;
GO

-- ---------------------------------------------------------------------------
-- reporting_reader: read the reports and the refresh log, nothing else.
-- No login is mapped to it yet: Grafana and Atlas get their own logins
-- (added to this role) in later phases.
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'reporting_reader' AND type = 'R')
    CREATE ROLE reporting_reader;
GO
-- On the whole schema: a report table added later is readable without a new grant.
GRANT SELECT ON SCHEMA::rpt TO reporting_reader;
GRANT SELECT ON OBJECT::ops.refresh_runs TO reporting_reader;
GO

/*
    What reporting_reader can NOT do: change any table, run the refresh
    (no EXECUTE), or see SalesDB at all (it's a role inside ReportingDB only).
*/
