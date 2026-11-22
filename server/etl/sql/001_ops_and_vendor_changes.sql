/*
    server/etl/sql/001_ops_and_vendor_changes.sql
    SalesDB objects for the vendor ETL (Phase 4). Run as the SQL admin with
    sqlcmd (see server/etl/README.md). Safe to re-run: every change checks
    first and keeps existing data.

    NOT part of shoplite/db/apply_schema.py on purpose: its script 001 drops
    and recreates SalesDB, which must never happen in production. (Locally,
    after an apply_schema.py rebuild, run this script again.)

    Creates or changes:
      1. Schema ops, and ops.etl_runs: one row per ETL run. Atlas reads it.
      2. vendor.supplier_prices: is_discontinued + discontinued_at, and an
         index on product_id (flagged as a likely need in 003_indexes.sql).
      3. vendor.supplier_prices_staging: where each run's extract lands.
      4. vendor.usp_clear_supplier_prices_staging: empties staging.
      5. vendor.usp_merge_supplier_prices: staging -> supplier_prices in one
         short transaction (insert new, update changed, mark discontinued),
         returning the counts.

    Permissions (002_etl_login.sql) let shoplite_etl insert into staging and
    run the two procedures, nothing more. The procedures read dbo.products
    and change vendor.supplier_prices through OWNERSHIP CHAINING: dbo owns
    the procedures, the vendor and ops schemas, and dbo.products, so SQL
    Server doesn't re-check the caller's permissions on objects the
    procedure touches. The ETL login can run the merge without being able to
    change supplier_prices any other way.
*/

USE SalesDB;
GO
SET NOCOUNT ON;
GO

-- ---------------------------------------------------------------------------
-- 1. ops schema and the run log
-- ---------------------------------------------------------------------------
IF SCHEMA_ID(N'ops') IS NULL
    EXEC (N'CREATE SCHEMA ops AUTHORIZATION dbo;');   -- CREATE SCHEMA must be alone in its batch
GO

IF OBJECT_ID(N'ops.etl_runs', N'U') IS NULL
BEGIN
    CREATE TABLE ops.etl_runs
    (
        run_id             INT IDENTITY(1,1) NOT NULL,
        feed               VARCHAR(50)    NOT NULL CONSTRAINT DF_etl_runs_feed DEFAULT ('vendor-supplier-feed'),
        contract_version   VARCHAR(20)    NULL,           -- e.g. 1.0.0, from the contract file
        started_at         DATETIME2(3)   NOT NULL CONSTRAINT DF_etl_runs_started_at DEFAULT (SYSUTCDATETIME()),
        finished_at        DATETIME2(3)   NULL,
        status             VARCHAR(20)    NOT NULL CONSTRAINT DF_etl_runs_status DEFAULT ('running'),
        -- Counts (NULL until the stage that sets them has run)
        rows_read          INT            NULL,           -- catalog rows read from the vendor
        rows_skipped       INT            NULL,           -- vendor SKUs that aren't ShopLite products
        rows_inserted      INT            NULL,
        rows_updated       INT            NULL,
        rows_discontinued  INT            NULL,
        rows_unchanged     INT            NULL,
        vendor_newest_price_at DATETIME2(3) NULL,         -- freshness evidence (UTC)
        -- On failure: which stage, and what happened
        error_stage        VARCHAR(20)    NULL,           -- connect, validate, extract, stage, merge
        error_message      NVARCHAR(2000) NULL,
        run_host           NVARCHAR(128)  NULL,           -- where the ETL ran

        CONSTRAINT PK_etl_runs PRIMARY KEY (run_id),
        CONSTRAINT CK_etl_runs_status CHECK (status IN ('running', 'succeeded', 'failed')),
        CONSTRAINT CK_etl_runs_error_stage CHECK (error_stage IS NULL
            OR error_stage IN ('connect', 'validate', 'extract', 'stage', 'merge'))
    );
    -- "The latest runs" is the common question (Atlas, the runbook).
    CREATE INDEX IX_etl_runs_started_at ON ops.etl_runs (started_at DESC);
END;
GO

-- ---------------------------------------------------------------------------
-- 2. vendor.supplier_prices: discontinued flag and a product_id index
-- ---------------------------------------------------------------------------
IF COL_LENGTH(N'vendor.supplier_prices', N'is_discontinued') IS NULL
    ALTER TABLE vendor.supplier_prices ADD
        is_discontinued BIT NOT NULL CONSTRAINT DF_supplier_prices_is_discontinued DEFAULT (0);
GO
IF COL_LENGTH(N'vendor.supplier_prices', N'discontinued_at') IS NULL
    ALTER TABLE vendor.supplier_prices ADD discontinued_at DATETIME2(3) NULL;
GO
-- Finds a product's suppliers (e.g. cheapest active supplier per product).
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_supplier_prices_product_id'
                 AND object_id = OBJECT_ID(N'vendor.supplier_prices'))
    CREATE INDEX IX_supplier_prices_product_id
        ON vendor.supplier_prices (product_id)
        INCLUDE (unit_cost, supplier_stock, is_discontinued);
GO

-- ---------------------------------------------------------------------------
-- 3. Staging: one run's extract, emptied at the start of each run
-- ---------------------------------------------------------------------------
IF OBJECT_ID(N'vendor.supplier_prices_staging', N'U') IS NULL
    CREATE TABLE vendor.supplier_prices_staging
    (
        supplier_name   NVARCHAR(100) NOT NULL,
        supplier_sku    VARCHAR(40)   NOT NULL,
        shoplite_sku    VARCHAR(20)   NOT NULL,
        unit_cost       DECIMAL(10,2) NOT NULL,
        supplier_stock  INT           NOT NULL,
        is_discontinued BIT           NOT NULL,
        -- The same key as the target: a duplicate in the vendor's data fails
        -- the stage step loudly instead of confusing the MERGE.
        CONSTRAINT PK_supplier_prices_staging PRIMARY KEY (supplier_name, supplier_sku)
    );
GO

-- ---------------------------------------------------------------------------
-- 4. Clear staging (DELETE rather than TRUNCATE: TRUNCATE needs ALTER
--    permission, DELETE works through ownership chaining)
-- ---------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE vendor.usp_clear_supplier_prices_staging
AS
BEGIN
    SET NOCOUNT ON;
    DELETE FROM vendor.supplier_prices_staging;
END;
GO

-- ---------------------------------------------------------------------------
-- 5. The merge
-- ---------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE vendor.usp_merge_supplier_prices
    @min_rows INT                -- the contract's minimum; refuse anything smaller
AS
/*
    One short transaction. For each staged row whose shoplite_sku is a ShopLite
    product (dbo.products.sku):
      - new (supplier_name, supplier_sku)  -> INSERT
      - existing and something changed     -> UPDATE (cost, stock, product,
                                              discontinued flag)
      - existing and nothing changed       -> left alone (so a second run with
                                              the same data changes nothing:
                                              idempotent)
    Rows in supplier_prices that are NOT in the feed any more, and not yet
    discontinued -> marked discontinued, stock 0 (kept, never deleted).

    Staged rows with an unknown SKU are skipped and counted.

    Safety: refuses (THROW 50101) if staging has fewer than @min_rows rows,
    so an empty or half-loaded staging table can't discontinue the catalog.

    Returns one row: rows_inserted, rows_updated, rows_discontinued,
    rows_unchanged, rows_skipped.
*/
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;           -- any error rolls the whole merge back

    DECLARE @staged INT = (SELECT COUNT(*) FROM vendor.supplier_prices_staging);
    IF @staged < @min_rows
    BEGIN
        DECLARE @message NVARCHAR(400) = CONCAT(
            N'Refusing to merge: staging has ', @staged, N' rows, fewer than the contract minimum of ',
            @min_rows, N'. Nothing was changed.');
        THROW 50101, @message, 1;
    END;

    DECLARE @skipped INT = (
        SELECT COUNT(*) FROM vendor.supplier_prices_staging AS s
        WHERE NOT EXISTS (SELECT 1 FROM dbo.products AS p WHERE p.sku = s.shoplite_sku));

    -- source_sku is NULL for rows changed because they VANISHED from the feed
    -- (WHEN NOT MATCHED BY SOURCE): that's how those updates are told apart
    -- from updates that came from the feed.
    DECLARE @changes TABLE (action NVARCHAR(10), source_sku VARCHAR(40) NULL,
                            was_discontinued BIT NULL, now_discontinued BIT NULL);

    BEGIN TRANSACTION;

    -- HOLDLOCK: no other session can slip a conflicting row in between
    -- MERGE's "does it exist?" check and its insert.
    MERGE vendor.supplier_prices WITH (HOLDLOCK) AS t
    USING (
        SELECT s.supplier_name, s.supplier_sku, p.product_id, s.unit_cost, s.supplier_stock, s.is_discontinued
        FROM vendor.supplier_prices_staging AS s
        JOIN dbo.products AS p ON p.sku = s.shoplite_sku
    ) AS src
       ON t.supplier_name = src.supplier_name
      AND t.supplier_sku  = src.supplier_sku
    WHEN MATCHED AND (   t.product_id      <> src.product_id
                      OR t.unit_cost       <> src.unit_cost
                      OR t.supplier_stock  <> src.supplier_stock
                      OR t.is_discontinued <> src.is_discontinued) THEN
        UPDATE SET
            product_id      = src.product_id,
            unit_cost       = src.unit_cost,
            supplier_stock  = src.supplier_stock,
            is_discontinued = src.is_discontinued,
            discontinued_at = CASE WHEN src.is_discontinued = 0 THEN NULL
                                   WHEN t.is_discontinued = 1 THEN t.discontinued_at
                                   ELSE SYSUTCDATETIME() END,
            updated_at      = SYSUTCDATETIME()
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (supplier_name, supplier_sku, product_id, unit_cost, supplier_stock,
                is_discontinued, discontinued_at, updated_at)
        VALUES (src.supplier_name, src.supplier_sku, src.product_id, src.unit_cost, src.supplier_stock,
                src.is_discontinued, CASE WHEN src.is_discontinued = 1 THEN SYSUTCDATETIME() END,
                SYSUTCDATETIME())
    WHEN NOT MATCHED BY SOURCE AND t.is_discontinued = 0 THEN
        UPDATE SET
            is_discontinued = 1,
            discontinued_at = SYSUTCDATETIME(),
            supplier_stock  = 0,
            updated_at      = SYSUTCDATETIME()
    OUTPUT $action, src.supplier_sku, deleted.is_discontinued, inserted.is_discontinued INTO @changes;

    COMMIT TRANSACTION;

    DECLARE @inserted INT = (SELECT COUNT(*) FROM @changes WHERE action = N'INSERT');
    -- Updates driven by the feed (the row is in staging).
    DECLARE @feed_updates INT = (SELECT COUNT(*) FROM @changes
                                 WHERE action = N'UPDATE' AND source_sku IS NOT NULL);
    -- Newly discontinued: vanished from the feed, or flagged by the vendor.
    DECLARE @discontinued INT = (SELECT COUNT(*) FROM @changes
                                 WHERE action = N'UPDATE' AND was_discontinued = 0 AND now_discontinued = 1);
    DECLARE @feed_discontinued INT = (SELECT COUNT(*) FROM @changes
                                      WHERE action = N'UPDATE' AND source_sku IS NOT NULL
                                        AND was_discontinued = 0 AND now_discontinued = 1);

    SELECT @inserted                                          AS rows_inserted,
           @feed_updates - @feed_discontinued                 AS rows_updated,
           @discontinued                                      AS rows_discontinued,
           (@staged - @skipped) - @inserted - @feed_updates   AS rows_unchanged,
           @skipped                                           AS rows_skipped;
END;
GO
