/*
    001_create_database.sql
    Creates an empty SalesDB with its database-level settings.

    !!! DESTRUCTIVE: if SalesDB already exists, this script disconnects
    !!! everyone using it and DROPS it, deleting all its data.
    !!! Intended for local development only. Never run against production.

    Run by apply_schema.py as sa. Batches are separated by GO.
    CREATE/DROP DATABASE can't run inside a transaction, so the runner
    executes these batches with autocommit on.
*/

-- A database can't be dropped while you're connected to it, so work from master.
USE master;
GO

-- Drop the existing SalesDB, if any.
-- SINGLE_USER WITH ROLLBACK IMMEDIATE rolls back open transactions and
-- disconnects other sessions (for example a VS Code query window), which
-- would otherwise make DROP DATABASE fail with "database is in use".
IF DB_ID(N'SalesDB') IS NOT NULL
BEGIN
    ALTER DATABASE SalesDB SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE SalesDB;
END;
GO

-- Default file locations and sizes are fine locally; they live in the
-- shoplite-sql-data Docker volume (/var/opt/mssql).
CREATE DATABASE SalesDB;
GO

-- SIMPLE recovery: the transaction log is reused automatically after each
-- checkpoint, so it doesn't grow without log backups. Right for a local copy
-- with nothing to recover. Production switches to FULL in Phase 4 so log
-- backups allow point-in-time restore.
ALTER DATABASE SalesDB SET RECOVERY SIMPLE;
GO

-- Query Store records every query's text, plans, and runtime statistics
-- inside the database. Atlas will rely on it to spot slow queries and plan
-- regressions. SQL Server 2022 turns it on for new databases by default;
-- the settings are stated explicitly so they're visible and don't depend on
-- defaults changing.
ALTER DATABASE SalesDB SET QUERY_STORE = ON;
GO

ALTER DATABASE SalesDB SET QUERY_STORE
(
    OPERATION_MODE = READ_WRITE,            -- actively capture (READ_ONLY would freeze it)
    QUERY_CAPTURE_MODE = AUTO,              -- skip trivial one-off queries, keep meaningful ones
    INTERVAL_LENGTH_MINUTES = 15,           -- stats bucket size; 15 min (default 60) gives finer incident timelines
    MAX_STORAGE_SIZE_MB = 1024,             -- cap Query Store's space inside SalesDB
    SIZE_BASED_CLEANUP_MODE = AUTO,         -- remove oldest data when near the cap, instead of switching to READ_ONLY
    CLEANUP_POLICY = (STALE_QUERY_THRESHOLD_DAYS = 30)
);
GO

/*
    Deliberately NOT changed: READ_COMMITTED_SNAPSHOT stays OFF (the SQL
    Server default). With it off, readers wait for writers' locks, so the
    blocking scenarios in Phase 5 behave the way they do on many real
    SQL Server systems. Turning it on would be a valid fix to discuss then,
    not a starting assumption.

    Timestamps are stored in UTC by the table defaults in 002_tables.sql
    (SYSUTCDATETIME()); there is no database-level time zone setting.
*/
