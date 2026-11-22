/*
    server/jobs/sql/040_jobs.sql
    Creates or updates every SQL Agent job and schedule for Phase 4. Run as
    the SQL admin after 010, 020, Ola's install into DBA, and 030.

    Idempotent: each job is created if missing, or reset to exactly what this
    script says (steps and schedules replaced, run history KEPT, via
    DBA.dbo.usp_prepare_job). Change a time here, re-run, done.

    Three sqlcmd variables:
      BACKUP_TARGET  URL (production, Blob Storage) or DISK (local)
      BACKUP_ROOT    the container URL, or a folder (local: /var/opt/mssql/backup)
      ETL_REPO_ROOT  C:\atlas on prod-sql-01 (creates the ETL job), or NONE
                     locally: the local ETL runs by hand from the Mac, and
                     SQL Agent on Linux can't run command steps anyway.

    Production:
      sqlcmd -S localhost -E -I -b -v BACKUP_TARGET=URL
             BACKUP_ROOT=https://<account>.blob.core.windows.net/sqlbackups
             ETL_REPO_ROOT=C:\atlas -i server\jobs\sql\040_jobs.sql

    All times are UTC, inside the VM's 13:00-23:00 weekday window (nothing
    may depend on the VM running at night or at weekends). The full table,
    with Boston times, is server/jobs/schedule.md:

      every 15 min 13:15-22:45, and 22:50   SalesDB log backup     (weekdays)
      14:00                                 Vendor ETL             (weekdays, VM only)
      14:30                                 ReportingDB refresh    (weekdays)
      17:00                                 SalesDB differential   (weekdays)
      20:00                                 Restore test           (Wednesday)
      20:30                                 ReportingDB full refresh (Friday)
      21:00                                 Integrity check        (Thursday)
      21:30                                 SalesDB full backup    (weekdays)
      21:45                                 System DBs, ReportingDB, DBA full backup (Friday)
      22:00                                 Index and statistics maintenance (weekdays)
      22:35                                 History cleanup        (weekdays)

    The backups, maintenance, and integrity check call Ola Hallengren's
    procedures in DBA, which log every command to DBA.dbo.CommandLog.
    No email alerts (no Database Mail): failures are in job history,
    CommandLog, ops.etl_runs, ops.refresh_runs, and restore_test_runs,
    which monitoring (Phase 5) and Atlas watch.
*/

USE DBA;
GO
SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

DECLARE @target    NVARCHAR(10)  = UPPER(N'$(BACKUP_TARGET)');
DECLARE @root      NVARCHAR(400) = N'$(BACKUP_ROOT)';
DECLARE @repo_root NVARCHAR(400) = N'$(ETL_REPO_ROOT)';
IF RIGHT(@root, 1) IN (N'/', N'\') SET @root = LEFT(@root, LEN(@root) - 1);
IF RIGHT(@repo_root, 1) = N'\' SET @repo_root = LEFT(@repo_root, LEN(@repo_root) - 1);

-- ---------------------------------------------------------------------------
-- Checks: create nothing unless everything the jobs call exists.
-- ---------------------------------------------------------------------------
IF @target NOT IN (N'URL', N'DISK')
    THROW 50501, N'BACKUP_TARGET must be URL or DISK. Nothing was changed.', 1;
IF @target = N'URL' AND NOT EXISTS (SELECT 1 FROM sys.credentials WHERE name = @root)
    THROW 50502, N'No credential named after BACKUP_ROOT: run server/backups/01-create-backup-credential.ps1 first. Nothing was changed.', 1;
IF OBJECT_ID(N'dbo.DatabaseBackup') IS NULL OR OBJECT_ID(N'dbo.IndexOptimize') IS NULL
   OR OBJECT_ID(N'dbo.DatabaseIntegrityCheck') IS NULL OR OBJECT_ID(N'dbo.CommandLog') IS NULL
    THROW 50503, N'Ola Hallengren''s procedures aren''t in DBA: install them first (server/jobs/README.md). Nothing was changed.', 1;
IF OBJECT_ID(N'dbo.usp_restore_test') IS NULL OR OBJECT_ID(N'dbo.usp_prepare_job') IS NULL
    THROW 50504, N'Run 020_dba_database.sql and 030_restore_test.sql first. Nothing was changed.', 1;
IF DB_ID(N'ReportingDB') IS NULL OR OBJECT_ID(N'ReportingDB.rpt.usp_refresh_sales') IS NULL
    THROW 50505, N'ReportingDB isn''t set up: run server/reporting/sql first. Nothing was changed.', 1;

-- ---------------------------------------------------------------------------
-- Job history: SQL Agent keeps only 1,000 rows (100 per job) by default.
-- The log backup job alone runs 40 times a day, so history would cover
-- barely two days. Keep plenty; the cleanup job trims it by AGE (30 days).
-- (Stored in the registry on Windows; SQL Server on Linux doesn't support
-- this procedure, so locally the defaults stay. Local history is short.)
-- ---------------------------------------------------------------------------
BEGIN TRY
    EXEC msdb.dbo.sp_set_sqlagent_properties @jobhistory_max_rows = 50000, @jobhistory_max_rows_per_job = 5000;
    PRINT N'Job history limits: 50,000 rows, 5,000 per job.';
END TRY
BEGIN CATCH
    PRINT N'Job history limits unchanged (not supported here): ' + ERROR_MESSAGE();
END CATCH;

-- ---------------------------------------------------------------------------
-- The part of every Ola backup command that says WHERE:
--   URL:  @URL, plus Microsoft's recommended block-blob settings. No
--         cleanup parameter: the storage lifecycle rule deletes files
--         after 14 days (Ola can't clean up URL backups).
--   DISK: @Directory, and Ola deletes files older than 14 days (336 hours)
--         after each successful backup.
-- Every backup: compressed, checksummed, verified (re-read) after writing,
-- logged to CommandLog.
-- ---------------------------------------------------------------------------
DECLARE @where NVARCHAR(600) = CASE WHEN @target = N'URL'
    THEN N'@URL = ' + dbo.fn_sql_literal(@root) + N', @MaxTransferSize = 4194304, @BlockSize = 65536'
    ELSE N'@Directory = ' + dbo.fn_sql_literal(@root) + N', @CleanupTime = 336, @CleanupMode = N''AFTER_BACKUP'''
END;
DECLARE @backup_options NVARCHAR(200) = N', @Compress = N''Y'', @Checksum = N''Y'', @Verify = N''Y'', @LogToTable = N''Y'';';

DECLARE @job_id UNIQUEIDENTIFIER, @command NVARCHAR(MAX);

-- ===========================================================================
-- Backups (SalesDB: FULL recovery; log chain started in B4)
-- ===========================================================================

-- Full: the base of each day's restores.
SET @command = N'EXECUTE dbo.DatabaseBackup @Databases = N''SalesDB'', @BackupType = N''FULL'', ' + @where + @backup_options;
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - Backup SalesDB FULL', @job_id = @job_id OUTPUT,
    @description = N'Full backup of SalesDB (Ola DatabaseBackup). The base of every restore that day.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'Full backup', @subsystem = N'TSQL',
    @database_name = N'DBA', @command = @command;
EXEC dbo.usp_add_job_schedule @job_id, N'Weekdays 21:30 UTC', 'weekdays', '21:30';

-- Differential: changes since the last full. Restoring the day's diff
-- skips replaying every log backup before it (faster restores).
-- ChangeBackupType: if no full exists (e.g. after a rebuild), take a full instead of failing.
SET @command = N'EXECUTE dbo.DatabaseBackup @Databases = N''SalesDB'', @BackupType = N''DIFF'', @ChangeBackupType = N''Y'', '
             + @where + @backup_options;
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - Backup SalesDB DIFF', @job_id = @job_id OUTPUT,
    @description = N'Differential backup of SalesDB: changes since the last full.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'Differential backup', @subsystem = N'TSQL',
    @database_name = N'DBA', @command = @command;
EXEC dbo.usp_add_job_schedule @job_id, N'Weekdays 17:00 UTC', 'weekdays', '17:00';

-- Log: every 15 minutes during business hours (RPO about 15 minutes), plus
-- one final backup at 22:50, after the 22:30 restock and the last regular
-- one, so the day's last changes are in Blob Storage before the VM stops at 23:00.
SET @command = N'EXECUTE dbo.DatabaseBackup @Databases = N''SalesDB'', @BackupType = N''LOG'', @ChangeBackupType = N''Y'', '
             + @where + @backup_options;
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - Backup SalesDB LOG', @job_id = @job_id OUTPUT,
    @description = N'Transaction log backup of SalesDB: every 15 minutes in business hours, and a final one at 22:50.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'Log backup', @subsystem = N'TSQL',
    @database_name = N'DBA', @command = @command;
EXEC dbo.usp_add_job_schedule @job_id, N'Weekdays every 15 min 13:15-22:45 UTC', 'weekdays', '13:15',
    @every_minutes = 15, @until = '22:45';
EXEC dbo.usp_add_job_schedule @job_id, N'Weekdays final 22:50 UTC', 'weekdays', '22:50';

-- Weekly: the system databases (master: logins and the backup credential;
-- msdb: these jobs and their history), ReportingDB (rebuildable, SIMPLE
-- recovery, so weekly is enough), and DBA.
SET @command = N'EXECUTE dbo.DatabaseBackup @Databases = N''SYSTEM_DATABASES, ReportingDB, DBA'', @BackupType = N''FULL'', '
             + @where + @backup_options;
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - Backup system, ReportingDB, DBA FULL', @job_id = @job_id OUTPUT,
    @description = N'Weekly full backup of master, model, msdb, ReportingDB, and DBA.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'Full backups', @subsystem = N'TSQL',
    @database_name = N'DBA', @command = @command;
EXEC dbo.usp_add_job_schedule @job_id, N'Friday 21:45 UTC', 'friday', '21:45';

-- ===========================================================================
-- Restore test (Wednesday: restores Tuesday night's full, Wednesday's diff,
-- and the logs since: exercises all three backup types)
-- ===========================================================================
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - Restore test', @job_id = @job_id OUTPUT,
    @description = N'Restores SalesDB''s latest full + diff + logs to SalesDB_RestoreTest, runs CHECKDB, records the result in DBA.dbo.restore_test_runs, drops the copy.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'Restore and check', @subsystem = N'TSQL',
    @database_name = N'DBA', @command = N'EXECUTE dbo.usp_restore_test;';
EXEC dbo.usp_add_job_schedule @job_id, N'Wednesday 20:00 UTC', 'wednesday', '20:00';

-- ===========================================================================
-- Maintenance
-- ===========================================================================

-- Integrity: DBCC CHECKDB reads every page and checks every structure.
-- Thursday, not Friday: Friday evening already has the flash sale's aftermath,
-- the ReportingDB full refresh, and the weekly backups; spreading the load
-- keeps the burstable VM's CPU credits healthy. The small databases are checked too.
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - Integrity check', @job_id = @job_id OUTPUT,
    @description = N'DBCC CHECKDB (Ola DatabaseIntegrityCheck) on SalesDB, ReportingDB, DBA, and the system databases.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'CHECKDB', @subsystem = N'TSQL',
    @database_name = N'DBA',
    @command = N'EXECUTE dbo.DatabaseIntegrityCheck @Databases = N''SYSTEM_DATABASES, SalesDB, ReportingDB, DBA'', @CheckCommands = N''CHECKDB'', @LogToTable = N''Y'';';
EXEC dbo.usp_add_job_schedule @job_id, N'Thursday 21:00 UTC', 'thursday', '21:00';

-- Indexes and statistics, Ola's widely used thresholds:
--   under 5% fragmented or under 1,000 pages: left alone (not worth it);
--   5-30%: reorganize (or rebuild online if it can't be reorganized);
--   over 30%: rebuild, online where possible (Developer edition allows it),
--   so the storefront keeps working during it.
-- Statistics: only those whose data changed (OnlyModifiedStatistics).
-- TimeLimit 1800: starts no new command after 30 minutes, so it's done
-- well before the VM stops at 23:00.
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - Index and statistics maintenance', @job_id = @job_id OUTPUT,
    @description = N'Ola IndexOptimize on the user databases: reorganize/rebuild fragmented indexes, update changed statistics.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'IndexOptimize', @subsystem = N'TSQL',
    @database_name = N'DBA',
    @command = N'EXECUTE dbo.IndexOptimize
    @Databases = N''USER_DATABASES'',
    @FragmentationLow = NULL,
    @FragmentationMedium = N''INDEX_REORGANIZE,INDEX_REBUILD_ONLINE,INDEX_REBUILD_OFFLINE'',
    @FragmentationHigh = N''INDEX_REBUILD_ONLINE,INDEX_REBUILD_OFFLINE'',
    @FragmentationLevel1 = 5,
    @FragmentationLevel2 = 30,
    @MinNumberOfPages = 1000,
    @UpdateStatistics = N''ALL'',
    @OnlyModifiedStatistics = N''Y'',
    @TimeLimit = 1800,
    @LogToTable = N''Y'';';
EXEC dbo.usp_add_job_schedule @job_id, N'Weekdays 22:00 UTC', 'weekdays', '22:00';

-- History cleanup: keep 30 days of backup history, job history, and
-- CommandLog (twice the backup retention, so the restore test always finds
-- the history of every backup that still exists).
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - History cleanup', @job_id = @job_id OUTPUT,
    @description = N'Deletes backup history, job history, and DBA.dbo.CommandLog rows older than 30 days.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'Trim history', @subsystem = N'TSQL',
    @database_name = N'msdb',
    @command = N'DECLARE @oldest DATETIME = DATEADD(DAY, -30, GETDATE());
EXECUTE msdb.dbo.sp_delete_backuphistory @oldest_date = @oldest;
EXECUTE msdb.dbo.sp_purge_jobhistory @oldest_date = @oldest;
DELETE FROM DBA.dbo.CommandLog WHERE StartTime < @oldest;';
EXEC dbo.usp_add_job_schedule @job_id, N'Weekdays 22:35 UTC', 'weekdays', '22:35';

-- ===========================================================================
-- Data: the vendor ETL and the ReportingDB refreshes
-- ===========================================================================

-- ReportingDB, daily: after the ETL. Incremental (last 3 days, plus any gap).
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - ReportingDB refresh', @job_id = @job_id OUTPUT,
    @description = N'Incremental ReportingDB refresh (rpt.usp_refresh_sales): last 3 days plus any gap since the last success.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'Incremental refresh', @subsystem = N'TSQL',
    @database_name = N'ReportingDB', @command = N'EXECUTE rpt.usp_refresh_sales;';
EXEC dbo.usp_add_job_schedule @job_id, N'Weekdays 14:30 UTC', 'weekdays', '14:30';

-- ReportingDB, weekly full rebuild: picks up changes older than the
-- incremental window (e.g. an order cancelled weeks after it was paid).
EXEC dbo.usp_prepare_job @job_name = N'ShopLite - ReportingDB full refresh', @job_id = @job_id OUTPUT,
    @description = N'Full ReportingDB rebuild from all SalesDB history.';
EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'Full refresh', @subsystem = N'TSQL',
    @database_name = N'ReportingDB', @command = N'EXECUTE rpt.usp_refresh_sales @mode = N''full'';';
EXEC dbo.usp_add_job_schedule @job_id, N'Friday 20:30 UTC', 'friday', '20:30';

-- The ETL: a command step (CmdExec) running the ETL's own Python, as the
-- SQL Agent service account. Exit code 0 = success; any other code fails
-- the step, and the code shows in job history (see server/etl/README.md).
-- Output: the last run's full log in F:\Logs\jobs\etl.log (overwritten each
-- run; every run's summary is kept in SalesDB's ops.etl_runs).
IF @repo_root = N'NONE'
BEGIN
    IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = N'ShopLite - Vendor ETL')
    BEGIN
        EXEC msdb.dbo.sp_delete_job @job_name = N'ShopLite - Vendor ETL';
        PRINT N'Removed job: ShopLite - Vendor ETL (ETL_REPO_ROOT=NONE).';
    END
    ELSE
        PRINT N'Skipped job: ShopLite - Vendor ETL (ETL_REPO_ROOT=NONE: run the ETL by hand).';
END
ELSE
BEGIN
    SET @command = N'"' + @repo_root + N'\server\etl\.venv\Scripts\python.exe" "'
                 + @repo_root + N'\server\etl\run_etl.py"';
    EXEC dbo.usp_prepare_job @job_name = N'ShopLite - Vendor ETL', @job_id = @job_id OUTPUT,
        @description = N'Vendor supplier feed into SalesDB (server/etl/run_etl.py). Every run is recorded in SalesDB.ops.etl_runs.';
    EXEC msdb.dbo.sp_add_jobstep @job_id = @job_id, @step_name = N'Run the ETL', @subsystem = N'CmdExec',
        @command = @command, @success_code = 0,
        @output_file_name = N'F:\Logs\jobs\etl.log', @flags = 0;   -- 0 = overwrite the file each run
    EXEC dbo.usp_add_job_schedule @job_id, N'Weekdays 14:00 UTC', 'weekdays', '14:00';
END;

-- ---------------------------------------------------------------------------
-- What exists now.
-- ---------------------------------------------------------------------------
SELECT j.name AS job, j.enabled, s.name AS schedule,
       CASE WHEN s.freq_subday_type = 4 THEN CONCAT(N'every ', s.freq_subday_interval, N' min ') ELSE N'' END
       + STUFF(STUFF(RIGHT(CONCAT(N'000000', s.active_start_time), 6), 5, 2, N''), 3, 0, N':')
       + CASE WHEN s.freq_subday_type = 4
              THEN N'-' + STUFF(STUFF(RIGHT(CONCAT(N'000000', s.active_end_time), 6), 5, 2, N''), 3, 0, N':')
              ELSE N'' END AS utc_time,
       CASE s.freq_interval WHEN 62 THEN N'weekdays' WHEN 2 THEN N'Monday' WHEN 4 THEN N'Tuesday'
            WHEN 8 THEN N'Wednesday' WHEN 16 THEN N'Thursday' WHEN 32 THEN N'Friday' ELSE N'?' END AS days
FROM msdb.dbo.sysjobs AS j
JOIN msdb.dbo.syscategories AS c ON c.category_id = j.category_id AND c.name = N'ShopLite'
LEFT JOIN msdb.dbo.sysjobschedules AS js ON js.job_id = j.job_id
LEFT JOIN msdb.dbo.sysschedules AS s ON s.schedule_id = js.schedule_id
ORDER BY s.active_start_time, j.name;
GO
