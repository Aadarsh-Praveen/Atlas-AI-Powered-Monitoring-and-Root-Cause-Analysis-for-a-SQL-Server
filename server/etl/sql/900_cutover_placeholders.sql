/*
    server/etl/sql/900_cutover_placeholders.sql
    ONE-TIME CUTOVER: removes the placeholder supplier rows that Phase 1's
    seed.py put into vendor.supplier_prices, so that from the first ETL run on,
    that table holds exactly what the vendor publishes (the vendor database is
    the only source of truth from Phase 4).

    Run ONCE per environment (locally and on prod-sql-01), as the SQL admin,
    after 001 and BEFORE the first ETL run:

        sqlcmd ... -I -b -v CONFIRM=REPLACE_PLACEHOLDERS -i server/etl/sql/900_cutover_placeholders.sql

    (-b makes sqlcmd exit non-zero when a guard refuses, so you notice.)

    Guards (it changes nothing unless BOTH hold):
      1. CONFIRM is exactly REPLACE_PLACEHOLDERS (no accidental runs).
      2. ops.etl_runs has NO successful run yet. Once a real vendor feed has
         been loaded, this script refuses: it would delete real vendor data.
    After a successful ETL run, the table is managed only by the merge.
*/

USE SalesDB;
GO
SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF N'$(CONFIRM)' <> N'REPLACE_PLACEHOLDERS'
BEGIN
    RAISERROR(N'Not confirmed: run with -v CONFIRM=REPLACE_PLACEHOLDERS. Nothing was changed.', 16, 1);
    RETURN;
END;

IF OBJECT_ID(N'ops.etl_runs', N'U') IS NULL
BEGIN
    RAISERROR(N'ops.etl_runs is missing: run server/etl/sql/001_ops_and_vendor_changes.sql first. Nothing was changed.', 16, 1);
    RETURN;
END;

IF EXISTS (SELECT 1 FROM ops.etl_runs WHERE status = 'succeeded')
BEGIN
    RAISERROR(N'Refusing: the ETL has already loaded the real vendor feed (ops.etl_runs has a succeeded run). The cutover is only for the Phase 1 placeholders. Nothing was changed.', 16, 1);
    RETURN;
END;

DECLARE @before INT = (SELECT COUNT(*) FROM vendor.supplier_prices);

BEGIN TRANSACTION;
    DELETE FROM vendor.supplier_prices;      -- placeholders only: no real feed has been loaded yet
COMMIT TRANSACTION;

PRINT CONCAT(N'Cutover done: removed ', @before, N' placeholder supplier rows. ',
             N'vendor.supplier_prices is empty until the first ETL run loads the vendor feed.');
GO
