/*
    server/jobs/sql/020_dba_database.sql
    The DBA database: home of the server's own maintenance tooling, kept out
    of master and out of the application databases.

      - Ola Hallengren's maintenance solution is installed INTO it next
        (server/jobs/README.md, "Install Ola Hallengren's scripts"), so its
        procedures and its CommandLog table (every backup, index rebuild,
        and integrity check, with start, end, and errors) live here. Atlas
        will read DBA.dbo.CommandLog to see what maintenance ran and when.
      - The restore test (030) records its results here.
      - Two helper procedures, below, used by the jobs script (040) to
        create or update jobs idempotently.

    Run as the SQL admin. Safe to re-run: created only if missing (never
    dropped), procedures replaced with CREATE OR ALTER.
*/

USE master;
GO
IF DB_ID(N'DBA') IS NULL
    CREATE DATABASE DBA;
GO
-- SIMPLE: its contents are logs and tooling, protected by the weekly full
-- backup (040); point-in-time restore isn't needed.
ALTER DATABASE DBA SET RECOVERY SIMPLE;
GO
-- Owner: the built-in admin login by its ID (0x01), as for ReportingDB.
DECLARE @owner SYSNAME = SUSER_SNAME(0x01);
DECLARE @current_owner SYSNAME = (SELECT SUSER_SNAME(owner_sid) FROM sys.databases WHERE name = N'DBA');
IF @current_owner IS NULL OR @current_owner <> @owner
BEGIN
    DECLARE @statement NVARCHAR(400) = N'ALTER AUTHORIZATION ON DATABASE::DBA TO ' + QUOTENAME(@owner) + N';';
    EXEC sys.sp_executesql @statement;
END;
GO

USE DBA;
GO
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

/*
    dbo.usp_prepare_job: makes sure a job exists with these properties and
    NO steps or schedules, ready for the caller to add them.

      - Missing: creates it (owner sa, category ShopLite) on this server.
      - Exists:  updates its description, owner, category, and enabled flag,
                 and removes its steps and schedules.

    Why not simply drop and recreate the job: dropping deletes its run
    history, which is exactly what Atlas and you read to see whether last
    night's backups worked. Resetting steps and schedules keeps the history
    and still makes the jobs script the single source of truth: run it
    again and every job matches the script.

    Owner sa (looked up by ID 0x01, whatever its name, even though it's
    disabled): jobs owned by a personal login fail when that login goes.
*/
CREATE OR ALTER PROCEDURE dbo.usp_prepare_job
    @job_name    SYSNAME,
    @description NVARCHAR(512),
    @enabled     BIT = 1,
    @category    SYSNAME = N'ShopLite',
    @job_id      UNIQUEIDENTIFIER OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @owner SYSNAME = SUSER_SNAME(0x01);

    IF NOT EXISTS (SELECT 1 FROM msdb.dbo.syscategories WHERE name = @category AND category_class = 1)
        EXEC msdb.dbo.sp_add_category @class = N'JOB', @type = N'LOCAL', @name = @category;

    SET @job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = @job_name);

    IF @job_id IS NULL
    BEGIN
        EXEC msdb.dbo.sp_add_job
            @job_name = @job_name, @enabled = @enabled, @description = @description,
            @category_name = @category, @owner_login_name = @owner, @job_id = @job_id OUTPUT;
        -- A job only runs on a server it's targeted at: this one.
        EXEC msdb.dbo.sp_add_jobserver @job_id = @job_id, @server_name = N'(local)';
        PRINT CONCAT(N'Created job: ', @job_name);
    END
    ELSE
    BEGIN
        EXEC msdb.dbo.sp_update_job
            @job_id = @job_id, @enabled = @enabled, @description = @description,
            @category_name = @category, @owner_login_name = @owner;

        EXEC msdb.dbo.sp_delete_jobstep @job_id = @job_id, @step_id = 0;   -- 0 = all steps

        DECLARE @schedule_id INT;
        WHILE 1 = 1
        BEGIN
            SET @schedule_id = (SELECT TOP 1 schedule_id FROM msdb.dbo.sysjobschedules WHERE job_id = @job_id);
            IF @schedule_id IS NULL BREAK;
            EXEC msdb.dbo.sp_detach_schedule
                @job_id = @job_id, @schedule_id = @schedule_id, @delete_unused_schedule = 1;
        END;
        PRINT CONCAT(N'Updated job: ', @job_name, N' (history kept)');
    END;
END;
GO

/*
    dbo.usp_add_job_schedule: a schedule in plain terms instead of SQL
    Agent's bit masks.

      @days   'weekdays' (Mon-Fri), or one day name: 'monday' ... 'sunday'
      @at     start time (UTC: the VM and the container run in UTC)
      @every_minutes + @until   optional: repeat every N minutes until @until

    Examples:
      @days = 'weekdays', @at = '21:30'                      daily full backup
      @days = 'friday',   @at = '21:00'                      weekly CHECKDB
      @days = 'weekdays', @at = '13:15', @every_minutes = 15, @until = '22:45'

    Translated into SQL Agent's terms: freq_type 8 (weekly) with
    freq_interval a sum of day bits (Sunday 1, Monday 2, Tuesday 4,
    Wednesday 8, Thursday 16, Friday 32, Saturday 64; weekdays = 62), and
    times as HHMMSS integers (21:30 -> 213000).
*/
CREATE OR ALTER PROCEDURE dbo.usp_add_job_schedule
    @job_id        UNIQUEIDENTIFIER,
    @schedule_name SYSNAME,
    @days          VARCHAR(10),
    @at            TIME(0),
    @every_minutes INT = NULL,
    @until         TIME(0) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @day_bits INT = CASE LOWER(@days)
        WHEN 'weekdays'  THEN 62
        WHEN 'sunday'    THEN 1
        WHEN 'monday'    THEN 2
        WHEN 'tuesday'   THEN 4
        WHEN 'wednesday' THEN 8
        WHEN 'thursday'  THEN 16
        WHEN 'friday'    THEN 32
        WHEN 'saturday'  THEN 64
    END;
    IF @day_bits IS NULL
        THROW 50301, N'@days must be weekdays or a day name (monday ... sunday).', 1;
    IF (@every_minutes IS NULL) <> (@until IS NULL)
        THROW 50302, N'@every_minutes and @until go together.', 1;

    DECLARE @start INT = DATEPART(HOUR, @at) * 10000 + DATEPART(MINUTE, @at) * 100;
    DECLARE @end   INT = CASE WHEN @until IS NULL THEN 235959
                              ELSE DATEPART(HOUR, @until) * 10000 + DATEPART(MINUTE, @until) * 100 END;

    EXEC msdb.dbo.sp_add_jobschedule
        @job_id = @job_id,
        @name = @schedule_name,
        @enabled = 1,
        @freq_type = 8,                                         -- weekly, on the days below
        @freq_interval = @day_bits,
        @freq_recurrence_factor = 1,                            -- every week
        @freq_subday_type = CASE WHEN @every_minutes IS NULL THEN 1 ELSE 4 END,   -- 1 once, 4 minutes
        @freq_subday_interval = COALESCE(@every_minutes, 0),
        @active_start_time = @start,
        @active_end_time = @end;
END;
GO

PRINT N'DBA ready. Next: install Ola Hallengren''s scripts into it (server/jobs/README.md).';
GO
