/*
    server/etl/sql/002_etl_login.sql
    The shoplite_etl login and its least-privilege role, for the vendor ETL.
    Run after 001, as the SQL admin, passing the password as a sqlcmd variable
    (see server/etl/README.md):

        sqlcmd ... -I -b -v SHOPLITE_ETL_PASSWORD="$SHOPLITE_ETL_PASSWORD" -i server/etl/sql/002_etl_login.sql

    The password is never stored in this file; the same value goes into
    server/etl/.env (SALESDB_PASSWORD). Safe to re-run: the login is created
    only if missing, and its password is reset each run (also how to rotate it).

    Same pattern as shoplite/db/004_logins.sql (shoplite_app): a LOGIN to
    connect, a USER in SalesDB, permissions on a ROLE.
*/

USE master;
GO
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'shoplite_etl')
BEGIN
    CREATE LOGIN shoplite_etl WITH
        PASSWORD = N'$(SHOPLITE_ETL_PASSWORD)',
        DEFAULT_DATABASE = SalesDB,
        CHECK_POLICY = ON,        -- SQL Server's password complexity rules
        CHECK_EXPIRATION = OFF;   -- a job's password mustn't expire unannounced
END
ELSE
BEGIN
    ALTER LOGIN shoplite_etl WITH
        PASSWORD = N'$(SHOPLITE_ETL_PASSWORD)',
        DEFAULT_DATABASE = SalesDB,
        CHECK_POLICY = ON,
        CHECK_EXPIRATION = OFF;
    ALTER LOGIN shoplite_etl ENABLE;
END;
GO

USE SalesDB;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'shoplite_etl')
    CREATE USER shoplite_etl FOR LOGIN shoplite_etl WITH DEFAULT_SCHEMA = vendor;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'shoplite_etl_role' AND type = 'R')
    CREATE ROLE shoplite_etl_role;
GO

-- Exactly what the ETL does, object by object:
--   1. empty staging, then bulk-insert the extract into it
GRANT EXECUTE ON OBJECT::vendor.usp_clear_supplier_prices_staging TO shoplite_etl_role;
GRANT INSERT  ON OBJECT::vendor.supplier_prices_staging           TO shoplite_etl_role;
--   2. merge staging into vendor.supplier_prices (the procedure does the
--      reads and writes through ownership chaining; see 001)
GRANT EXECUTE ON OBJECT::vendor.usp_merge_supplier_prices          TO shoplite_etl_role;
--   3. record the run: insert a "running" row, then update it at the end
GRANT SELECT, INSERT, UPDATE ON OBJECT::ops.etl_runs              TO shoplite_etl_role;
GO
ALTER ROLE shoplite_etl_role ADD MEMBER shoplite_etl;
GO

/*
    What shoplite_etl can NOT do, because it was never granted:
      - Read or change vendor.supplier_prices directly (only through the merge).
      - Read or change anything in dbo (products, orders, customers...).
      - Delete from or truncate staging other than through the clear procedure.
      - Create, alter, or drop anything; change settings; read server DMVs.
    Check it (README, "Check the ETL login"): as shoplite_etl,
    SELECT TOP 1 * FROM dbo.orders must fail with "permission denied".
*/
