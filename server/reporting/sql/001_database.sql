/*
    server/reporting/sql/001_database.sql
    Creates ReportingDB: daily sales summaries rebuilt from SalesDB, for
    reporting now and for Grafana and forecasting later.

    Safe everywhere, including production: it creates the database only if
    it's MISSING and never drops anything (unlike shoplite/db/001, which is
    for local rebuilds only). Re-running only re-applies the settings.

    Run as the SQL admin (see server/reporting/README.md):
        sqlcmd ... -I -b -i server/reporting/sql/001_database.sql
*/

USE master;
GO

-- Files go to the server's default data and log folders: the Docker volume
-- locally, the data disk (F:) on prod-sql-01, set up in Phase 2.
IF DB_ID(N'ReportingDB') IS NULL
    CREATE DATABASE ReportingDB;
GO

-- SIMPLE recovery: everything here can be rebuilt from SalesDB with a full
-- refresh, so it needs no log backups (Phase 4 backs it up weekly, in full).
-- The log is reused after each checkpoint instead of growing until backed up.
ALTER DATABASE ReportingDB SET RECOVERY SIMPLE;
GO

-- Query Store, as in SalesDB: Atlas will see the refresh's queries and
-- plans, for example when a refresh suddenly takes three times longer.
-- A smaller cap than SalesDB's: few, repetitive queries run here.
ALTER DATABASE ReportingDB SET QUERY_STORE = ON;
GO
ALTER DATABASE ReportingDB SET QUERY_STORE
(
    OPERATION_MODE = READ_WRITE,
    QUERY_CAPTURE_MODE = AUTO,
    INTERVAL_LENGTH_MINUTES = 15,
    MAX_STORAGE_SIZE_MB = 256,
    SIZE_BASED_CLEANUP_MODE = AUTO,
    CLEANUP_POLICY = (STALE_QUERY_THRESHOLD_DAYS = 30)
);
GO

-- Owner: the built-in admin login (security ID 0x01, "sa", even if it's
-- disabled or renamed, as on Azure images), not whichever personal login
-- ran this script. A database owned by a login that's later dropped or
-- disabled causes confusing errors in jobs. Looked up by its ID, so this
-- works whatever the login is called.
--
-- Dynamic SQL, built in a variable first: ALTER AUTHORIZATION only takes a
-- written name, not a function call or variable, and EXEC (...) only
-- accepts string literals and variables joined with +, not a function
-- call such as QUOTENAME(...) inside the parentheses. QUOTENAME wraps the
-- name in [ ] (and doubles any ] in it), so it's always one valid name.
DECLARE @owner SYSNAME = SUSER_SNAME(0x01);
DECLARE @current_owner SYSNAME = (SELECT SUSER_SNAME(owner_sid) FROM sys.databases WHERE name = N'ReportingDB');
IF @current_owner IS NULL OR @current_owner <> @owner      -- NULL: owned by a login that no longer exists
BEGIN
    DECLARE @statement NVARCHAR(400) =
        N'ALTER AUTHORIZATION ON DATABASE::ReportingDB TO ' + QUOTENAME(@owner) + N';';
    EXEC sys.sp_executesql @statement;
END;
GO

PRINT N'ReportingDB ready (SIMPLE recovery, Query Store on).';
GO
