/*
    server/jobs/sql/010_server_settings_and_xe.sql
    Server-wide settings for Phase 4, and the Extended Events session.
    Run as the SQL admin. Safe to re-run.

    One sqlcmd variable, the event file's full path (its folder must exist):
      Production:  -v XE_FILE=F:\XE\ShopLite_Diagnostics.xel
                   (folder from server\jobs\01-prepare-server.ps1)
      Local:       -v XE_FILE=/var/opt/mssql/xe/ShopLite_Diagnostics.xel
                   (docker exec shoplite-sql mkdir -p /var/opt/mssql/xe)
*/

USE master;
GO
SET NOCOUNT ON;
GO

-- ---------------------------------------------------------------------------
-- 1. Settings (sp_configure). Each is read first and changed only if needed.
-- ---------------------------------------------------------------------------
-- The three below are "advanced" options, hidden until this is on.
IF (SELECT CAST(value_in_use AS INT) FROM sys.configurations WHERE name = N'show advanced options') <> 1
BEGIN
    EXEC sys.sp_configure N'show advanced options', 1;
    RECONFIGURE;
END;
GO

DECLARE @settings TABLE (name NVARCHAR(70), wanted INT, why NVARCHAR(200));
INSERT INTO @settings VALUES
    -- A session blocked for 10+ seconds produces a blocked process report
    -- (captured by the session below). 0, the default, means never.
    (N'blocked process threshold (s)', 10, N'blocking over 10 s is reported'),
    -- Every backup compressed and checksummed by default, including ad-hoc
    -- ones someone takes by hand without remembering the options.
    (N'backup compression default',    1,  N'backups compressed unless told otherwise'),
    (N'backup checksum default',       1,  N'backups verify page checksums unless told otherwise');

DECLARE @name NVARCHAR(70), @wanted INT, @why NVARCHAR(200);
DECLARE settings CURSOR LOCAL FAST_FORWARD FOR SELECT name, wanted, why FROM @settings;
OPEN settings;
FETCH NEXT FROM settings INTO @name, @wanted, @why;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF (SELECT CAST(value_in_use AS INT) FROM sys.configurations WHERE name = @name) <> @wanted
    BEGIN
        EXEC sys.sp_configure @name, @wanted;
        RECONFIGURE;
        PRINT CONCAT(N'Set ', @name, N' = ', @wanted, N' (', @why, N').');
    END
    ELSE
        PRINT CONCAT(@name, N' is already ', @wanted, N'.');
    FETCH NEXT FROM settings INTO @name, @wanted, @why;
END;
CLOSE settings;
DEALLOCATE settings;
GO

-- ---------------------------------------------------------------------------
-- 2. Extended Events: ShopLite_Diagnostics
--
-- Extended Events is SQL Server's lightweight tracing: the events below
-- cost almost nothing until they happen. Atlas will read these files to
-- explain deadlocks, blocking, slow requests, and errors.
--
--   xml_deadlock_report     the full deadlock graph: who held what, who waited
--   blocked_process_report  a session blocked over the threshold above, with
--                           both the blocker's and the blocked statement
--   rpc_completed           requests from applications (the API's
--   sql_batch_completed     parameterized queries arrive as RPCs; ad-hoc
--                           batches as batches) taking over 2 seconds
--                           (duration is in microseconds)
--   error_reported          errors of severity 16 or higher (user-correctable
--                           errors and worse; informational messages excluded)
--
-- Every event records which application, database, login, and host it came
-- from: client_app_name is the APP= tag (ShopLite-API, ShopLite-ETL, ...).
--
-- Target: rolling files, 50 MB each, at most 10 (500 MB in all on F:);
-- the oldest is deleted when a new one starts.
-- STARTUP_STATE = ON: the session starts with SQL Server, every morning.
-- ALLOW_SINGLE_EVENT_LOSS: under extreme load SQL Server may drop an event
-- rather than slow down queries to record it.
--
-- Recreated on every run (DROP + CREATE), so an edit here takes effect;
-- existing files stay on disk and the new session keeps adding to the set.
-- ---------------------------------------------------------------------------
IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = N'ShopLite_Diagnostics')
    DROP EVENT SESSION ShopLite_Diagnostics ON SERVER;
GO

CREATE EVENT SESSION ShopLite_Diagnostics ON SERVER
ADD EVENT sqlserver.xml_deadlock_report
(
    ACTION (sqlserver.client_app_name, sqlserver.client_hostname, sqlserver.database_name,
            sqlserver.server_principal_name)
),
ADD EVENT sqlserver.blocked_process_report
(
    ACTION (sqlserver.client_app_name, sqlserver.client_hostname, sqlserver.database_name,
            sqlserver.server_principal_name)
),
ADD EVENT sqlserver.rpc_completed
(
    ACTION (sqlserver.client_app_name, sqlserver.client_hostname, sqlserver.database_name,
            sqlserver.server_principal_name, sqlserver.query_hash, sqlserver.session_id)
    WHERE duration > 2000000                -- 2 seconds, in microseconds
),
ADD EVENT sqlserver.sql_batch_completed
(
    ACTION (sqlserver.client_app_name, sqlserver.client_hostname, sqlserver.database_name,
            sqlserver.server_principal_name, sqlserver.query_hash, sqlserver.session_id)
    WHERE duration > 2000000
),
ADD EVENT sqlserver.error_reported
(
    ACTION (sqlserver.client_app_name, sqlserver.client_hostname, sqlserver.database_name,
            sqlserver.server_principal_name, sqlserver.sql_text, sqlserver.session_id)
    WHERE severity >= 16
)
ADD TARGET package0.event_file
(
    SET filename = N'$(XE_FILE)', max_file_size = 50, max_rollover_files = 10
)
WITH
(
    MAX_MEMORY = 4096 KB,
    EVENT_RETENTION_MODE = ALLOW_SINGLE_EVENT_LOSS,
    MAX_DISPATCH_LATENCY = 30 SECONDS,      -- events reach the file within 30 s
    STARTUP_STATE = ON
);
GO

ALTER EVENT SESSION ShopLite_Diagnostics ON SERVER STATE = START;
GO

-- Proof: running, and where it writes.
SELECT s.name AS session_name, 'running' AS state,
       CAST(t.target_data AS XML).value('(EventFileTarget/File/@name)[1]', 'NVARCHAR(400)') AS current_file
FROM sys.dm_xe_sessions AS s
JOIN sys.dm_xe_session_targets AS t ON t.event_session_address = s.address
WHERE s.name = N'ShopLite_Diagnostics' AND t.target_name = N'event_file';
GO
