/*
    server/collector/sql/001_monitor_login.sql
    The shoplite_monitor login: what the monitoring collector (server/collector/)
    connects as. It can READ SQL Server's health information and the
    operational tables, and nothing else: no access to customer, order, or
    product data, and no write access anywhere.

    Run as the SQL admin, passing the password as a sqlcmd variable (never
    stored in this file; the same value goes into the collector's .env):

      Local:  docker exec -i -e MP="$SHOPLITE_MONITOR_PASSWORD" shoplite-sql sh -c
                '/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -I -b
                 -v SHOPLITE_MONITOR_PASSWORD="$MP"' < server/collector/sql/001_monitor_login.sql
      VM:     see server/collector/README.md (password asked for with Read-Host)

    Safe to re-run: the login is created if missing, otherwise its password
    is reset (also how to rotate it); users and grants are created only if
    missing. Run it after Phase 4 is set up (it grants on Phase 4's tables).

    Same pattern as shoplite_app and shoplite_etl: a LOGIN to connect, a USER
    in each database it needs, permissions granted explicitly.

    What each grant is for:
      Server     VIEW SERVER PERFORMANCE STATE
                                        the DMVs: waits, sessions, requests and
                                        blocking, transactions, performance
                                        counters, file I/O, memory; reading the
                                        Extended Events files
      SalesDB    VIEW DATABASE STATE    Query Store; space used in the database
                 SELECT ops.etl_runs    ETL outcomes
      ReportingDB VIEW DATABASE STATE   Query Store; space used
                 SELECT ops.refresh_runs ReportingDB refresh outcomes
      DBA        VIEW DATABASE STATE    space used
                 SELECT dbo.CommandLog, dbo.restore_test_runs
                                        maintenance commands and restore tests
      msdb       SELECT on the job and backup history tables, EXECUTE on
                 dbo.agent_datetime (turns msdb's integer dates into datetimes)

    Why VIEW SERVER PERFORMANCE STATE and not VIEW SERVER STATE: SQL Server
    2022 split VIEW SERVER STATE into two narrower permissions. PERFORMANCE
    STATE covers the performance DMVs above; SECURITY STATE covers the
    security-related ones (audits, encryption, connections' authentication
    details), which a monitoring collector has no reason to read. VIEW SERVER
    STATE is simply both. Decided with the owner: try the narrower one, and
    let the collector's local tests (server/collector/tests/) run EVERY
    collection query as shoplite_monitor to prove it's enough.
    Verified 2026-11-22: every query passed with VIEW SERVER PERFORMANCE
    STATE; none needs VIEW SERVER STATE. If a future query does, it goes back
    to VIEW SERVER STATE only then, and this comment names the query and why.
*/

USE master;
GO
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'shoplite_monitor')
BEGIN
    CREATE LOGIN shoplite_monitor WITH
        PASSWORD = N'$(SHOPLITE_MONITOR_PASSWORD)',
        DEFAULT_DATABASE = master,
        CHECK_POLICY = ON,          -- SQL Server's password complexity rules
        CHECK_EXPIRATION = OFF;     -- a service's password mustn't expire unannounced
END
ELSE
BEGIN
    ALTER LOGIN shoplite_monitor WITH
        PASSWORD = N'$(SHOPLITE_MONITOR_PASSWORD)',
        DEFAULT_DATABASE = master,
        CHECK_POLICY = ON,
        CHECK_EXPIRATION = OFF;
    ALTER LOGIN shoplite_monitor ENABLE;
END;
GO

-- Server-wide: read the DMVs that describe the server's performance. This
-- shows other sessions' current statements (that's how blocking is
-- diagnosed), but grants no access to any table's data.
GRANT VIEW SERVER PERFORMANCE STATE TO shoplite_monitor;
GO
-- If an earlier run of this script granted the broader permission, take it
-- back, so re-running the script really leaves only the narrower one.
IF EXISTS (SELECT 1 FROM sys.server_permissions AS p
           JOIN sys.server_principals AS l ON l.principal_id = p.grantee_principal_id
           WHERE l.name = N'shoplite_monitor' AND p.permission_name = N'VIEW SERVER STATE')
    REVOKE VIEW SERVER STATE FROM shoplite_monitor;
GO

-- ---------------------------------------------------------------------------
-- SalesDB
-- ---------------------------------------------------------------------------
USE SalesDB;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'shoplite_monitor')
    CREATE USER shoplite_monitor FOR LOGIN shoplite_monitor;
GO
GRANT VIEW DATABASE STATE TO shoplite_monitor;
GRANT SELECT ON OBJECT::ops.etl_runs TO shoplite_monitor;
GO

-- ---------------------------------------------------------------------------
-- ReportingDB
-- ---------------------------------------------------------------------------
USE ReportingDB;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'shoplite_monitor')
    CREATE USER shoplite_monitor FOR LOGIN shoplite_monitor;
GO
GRANT VIEW DATABASE STATE TO shoplite_monitor;
GRANT SELECT ON OBJECT::ops.refresh_runs TO shoplite_monitor;
GO

-- ---------------------------------------------------------------------------
-- DBA
-- ---------------------------------------------------------------------------
USE DBA;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'shoplite_monitor')
    CREATE USER shoplite_monitor FOR LOGIN shoplite_monitor;
GO
GRANT VIEW DATABASE STATE TO shoplite_monitor;
GRANT SELECT ON OBJECT::dbo.CommandLog        TO shoplite_monitor;
GRANT SELECT ON OBJECT::dbo.restore_test_runs TO shoplite_monitor;
GO

-- ---------------------------------------------------------------------------
-- msdb: job and backup history only (not SQLAgentReaderRole, which would
-- also let it see job step commands through the Agent procedures).
-- ---------------------------------------------------------------------------
USE msdb;
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'shoplite_monitor')
    CREATE USER shoplite_monitor FOR LOGIN shoplite_monitor;
GO
GRANT VIEW DATABASE STATE TO shoplite_monitor;
GRANT SELECT ON OBJECT::dbo.sysjobs           TO shoplite_monitor;
GRANT SELECT ON OBJECT::dbo.sysjobhistory     TO shoplite_monitor;
GRANT SELECT ON OBJECT::dbo.syscategories     TO shoplite_monitor;
GRANT SELECT ON OBJECT::dbo.backupset         TO shoplite_monitor;
GRANT SELECT ON OBJECT::dbo.backupmediafamily TO shoplite_monitor;
GRANT EXECUTE ON OBJECT::dbo.agent_datetime   TO shoplite_monitor;
GO

/*
    What shoplite_monitor can NOT do, because it was never granted:
      - Read any business data: dbo.orders, customers, products, inventory,
        vendor.supplier_prices, the ReportingDB report tables.
      - Insert, update, or delete anything; create, alter, or drop anything.
      - Start, stop, or change SQL Agent jobs; read job step commands.
      - KILL sessions, change server settings, read logins or permissions.

    Check (server/collector/README.md, "Check the monitor login"), as
    shoplite_monitor:
      SELECT TOP 1 * FROM SalesDB.dbo.orders;          -- must fail: permission denied
      SELECT COUNT(*) FROM sys.dm_exec_sessions;       -- works: sees every session
*/
