/*
    server/jobs/sql/030_restore_test.sql
    The weekly restore test: "a backup isn't a backup until you've restored it."

    dbo.usp_restore_test (in DBA) restores SalesDB's latest backups to a
    scratch database, checks it, records the result, and drops it:

      1. Finds, in msdb's backup history:
           - the latest full backup (not COPY_ONLY: those don't anchor a chain);
           - the latest differential based on THAT full (if any);
           - every log backup after it, in order.
         It checks the log chain is unbroken: each log backup must start
         exactly where the previous one ended. A gap means point-in-time
         restore is impossible past it, and the test FAILS saying so.
      2. Restores them as SalesDB_RestoreTest (files renamed, so SalesDB's
         files are never touched), from Blob Storage (URL) or disk, whichever
         the backups were written to, then brings it online.
      3. Runs DBCC CHECKDB on the copy: proves the restored data is
         consistent, not just that files could be read.
      4. Records everything in dbo.restore_test_runs (how long the restore
         took is your real RTO evidence), drops the copy, and re-raises any
         failure so the SQL Agent job shows red.

    Run as the SQL admin after 020 (and after Ola, though this doesn't use
    it). Safe to re-run: CREATE OR ALTER, table created only if missing.

    Manual run:  EXEC DBA.dbo.usp_restore_test;
*/

USE DBA;
GO
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- ---------------------------------------------------------------------------
-- Results: one row per test. Atlas reads it ("when did a restore last
-- succeed, and how long did it take?").
-- ---------------------------------------------------------------------------
IF OBJECT_ID(N'dbo.restore_test_runs', N'U') IS NULL
    CREATE TABLE dbo.restore_test_runs
    (
        run_id                  INT IDENTITY(1,1) NOT NULL,
        started_at              DATETIME2(0)   NOT NULL CONSTRAINT DF_restore_test_runs_started_at DEFAULT (SYSUTCDATETIME()),
        finished_at             DATETIME2(0)   NULL,
        status                  VARCHAR(20)    NOT NULL CONSTRAINT DF_restore_test_runs_status DEFAULT ('running'),
        source_database         SYSNAME        NOT NULL,
        restore_database        SYSNAME        NOT NULL,
        full_backup_file        NVARCHAR(1000) NULL,
        full_backup_finished_at DATETIME2(0)   NULL,
        diff_backup_file        NVARCHAR(1000) NULL,
        log_backups_restored    INT            NULL,
        restored_to             DATETIME2(0)   NULL,      -- end of the newest backup restored: how recent the copy is
        restore_seconds         INT            NULL,      -- RTO evidence
        checkdb_seconds         INT            NULL,
        error_message           NVARCHAR(2000) NULL,
        CONSTRAINT PK_restore_test_runs PRIMARY KEY (run_id),
        CONSTRAINT CK_restore_test_runs_status CHECK (status IN ('running', 'succeeded', 'failed'))
    );
GO

-- ---------------------------------------------------------------------------
-- A T-SQL string literal for dynamic SQL: N'...' with quotes doubled.
-- (QUOTENAME(x, '''') does the same but returns NULL past 128 characters,
-- and blob URLs are often longer.)
-- ---------------------------------------------------------------------------
CREATE OR ALTER FUNCTION dbo.fn_sql_literal (@value NVARCHAR(4000))
RETURNS NVARCHAR(4000)
AS
BEGIN
    RETURN N'N''' + REPLACE(@value, N'''', N'''''') + N'''';
END;
GO

CREATE OR ALTER PROCEDURE dbo.usp_restore_test
    @source_database  SYSNAME = N'SalesDB',
    @restore_database SYSNAME = N'SalesDB_RestoreTest'
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    -- Safety: the scratch name must end in _RestoreTest. This procedure
    -- drops and overwrites that database, so it must never be able to
    -- target a real one.
    IF @restore_database NOT LIKE N'%[_]RestoreTest' OR @restore_database = @source_database
        THROW 50401, N'@restore_database must end in _RestoreTest (it is overwritten and dropped).', 1;

    DECLARE @run_id INT;
    INSERT INTO dbo.restore_test_runs (source_database, restore_database)
    VALUES (@source_database, @restore_database);
    SET @run_id = SCOPE_IDENTITY();

    DECLARE @sql NVARCHAR(MAX), @restore_started DATETIME2(0), @checkdb_started DATETIME2(0);

    BEGIN TRY
        -- A leftover copy from a run that died halfway: remove it first.
        IF DB_ID(@restore_database) IS NOT NULL
        BEGIN
            SET @sql = N'ALTER DATABASE ' + QUOTENAME(@restore_database) + N' SET SINGLE_USER WITH ROLLBACK IMMEDIATE; '
                     + N'DROP DATABASE ' + QUOTENAME(@restore_database) + N';';
            EXEC sys.sp_executesql @sql;
        END;

        -- -------------------------------------------------------------------
        -- 1. The backups to restore, from msdb's history. device_type 2 =
        --    disk, 9 = URL (skips snapshot/virtual devices).
        -- -------------------------------------------------------------------
        DECLARE @backups TABLE
        (
            seq          INT IDENTITY(1,1) PRIMARY KEY,
            backup_type  CHAR(1),          -- D full, I differential, L log
            device       NVARCHAR(1000),
            first_lsn    NUMERIC(25,0),
            last_lsn     NUMERIC(25,0),
            finished_at  DATETIME2(0)
        );

        -- The latest full backup.
        DECLARE @full_set_id INT, @full_checkpoint_lsn NUMERIC(25,0);
        SELECT TOP 1 @full_set_id = b.backup_set_id, @full_checkpoint_lsn = b.checkpoint_lsn
        FROM msdb.dbo.backupset AS b
        WHERE b.database_name = @source_database AND b.type = 'D' AND b.is_copy_only = 0
        ORDER BY b.backup_finish_date DESC;
        IF @full_set_id IS NULL
            THROW 50402, N'No full backup of the source database in msdb''s history.', 1;

        INSERT INTO @backups (backup_type, device, first_lsn, last_lsn, finished_at)
        SELECT b.type, m.physical_device_name, b.first_lsn, b.last_lsn, b.backup_finish_date
        FROM msdb.dbo.backupset AS b
        JOIN msdb.dbo.backupmediafamily AS m ON m.media_set_id = b.media_set_id
        WHERE b.backup_set_id = @full_set_id AND m.device_type IN (2, 9);

        IF (SELECT COUNT(*) FROM @backups) <> 1
            THROW 50403, N'The latest full backup isn''t a single disk or URL file (striped or virtual device): this test doesn''t handle that.', 1;

        -- The latest differential based on that full (its differential_base_lsn
        -- is the full's checkpoint LSN), if there is one.
        INSERT INTO @backups (backup_type, device, first_lsn, last_lsn, finished_at)
        SELECT TOP 1 b.type, m.physical_device_name, b.first_lsn, b.last_lsn, b.backup_finish_date
        FROM msdb.dbo.backupset AS b
        JOIN msdb.dbo.backupmediafamily AS m ON m.media_set_id = b.media_set_id
        WHERE b.database_name = @source_database AND b.type = 'I' AND b.is_copy_only = 0
          AND b.differential_base_lsn = @full_checkpoint_lsn AND m.device_type IN (2, 9)
        ORDER BY b.backup_finish_date DESC;

        -- Every log backup that ends after the newest backup so far, oldest first.
        DECLARE @base_last_lsn NUMERIC(25,0) = (SELECT TOP 1 last_lsn FROM @backups ORDER BY seq DESC);
        INSERT INTO @backups (backup_type, device, first_lsn, last_lsn, finished_at)
        SELECT b.type, m.physical_device_name, b.first_lsn, b.last_lsn, b.backup_finish_date
        FROM msdb.dbo.backupset AS b
        JOIN msdb.dbo.backupmediafamily AS m ON m.media_set_id = b.media_set_id
        WHERE b.database_name = @source_database AND b.type = 'L' AND b.is_copy_only = 0
          AND b.last_lsn > @base_last_lsn AND m.device_type IN (2, 9)
        ORDER BY b.first_lsn;

        -- The chain: the first log must cover the base's last LSN, and each
        -- log must start exactly where the previous one ended.
        DECLARE @gap_at DATETIME2(0) = (
            SELECT TOP 1 cur.finished_at
            FROM @backups AS cur
            JOIN @backups AS prev ON prev.seq = (SELECT MAX(seq) FROM @backups WHERE seq < cur.seq)
            WHERE cur.backup_type = 'L'
              AND ((prev.backup_type = 'L' AND cur.first_lsn <> prev.last_lsn)
                OR (prev.backup_type <> 'L' AND cur.first_lsn > prev.last_lsn))
            ORDER BY cur.seq);
        IF @gap_at IS NOT NULL
        BEGIN
            DECLARE @gap_message NVARCHAR(400) = CONCAT(
                N'The log chain is broken: the log backup finished at ', FORMAT(@gap_at, 'yyyy-MM-dd HH:mm'),
                N' UTC doesn''t start where the previous backup ended. A log backup is missing, or the ',
                N'database was switched to SIMPLE recovery. Take a new full backup to start a new chain.');
            THROW 50404, @gap_message, 1;
        END;

        UPDATE dbo.restore_test_runs
        SET full_backup_file        = (SELECT device FROM @backups WHERE backup_type = 'D'),
            full_backup_finished_at = (SELECT finished_at FROM @backups WHERE backup_type = 'D'),
            diff_backup_file        = (SELECT device FROM @backups WHERE backup_type = 'I'),
            log_backups_restored    = (SELECT COUNT(*) FROM @backups WHERE backup_type = 'L'),
            restored_to             = (SELECT MAX(finished_at) FROM @backups)
        WHERE run_id = @run_id;

        -- -------------------------------------------------------------------
        -- 2. Restore. Every file of the database is MOVEd to a new name in
        --    the server's default folders (F:\SQLData, F:\SQLLog on the VM),
        --    so the copy can't collide with SalesDB's own files.
        -- -------------------------------------------------------------------
        DECLARE @data_path NVARCHAR(400) = CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS NVARCHAR(400));
        DECLARE @log_path  NVARCHAR(400) = CAST(SERVERPROPERTY('InstanceDefaultLogPath')  AS NVARCHAR(400));
        DECLARE @moves NVARCHAR(MAX) = (
            SELECT STRING_AGG(CAST(
                       N'MOVE ' + dbo.fn_sql_literal(f.logical_name) + N' TO '
                     + dbo.fn_sql_literal(CASE WHEN f.file_type = 'L' THEN @log_path ELSE @data_path END
                                          + @restore_database + N'_' + f.logical_name
                                          + CASE WHEN f.file_type = 'L' THEN N'.ldf' ELSE N'.mdf' END)
                   AS NVARCHAR(MAX)), N', ')
            FROM msdb.dbo.backupfile AS f
            WHERE f.backup_set_id = @full_set_id);

        SET @restore_started = SYSUTCDATETIME();

        DECLARE @seq INT = 1, @last_seq INT = (SELECT MAX(seq) FROM @backups);
        DECLARE @device NVARCHAR(1000), @type CHAR(1), @from NVARCHAR(1100);
        WHILE @seq <= @last_seq
        BEGIN
            SELECT @device = device, @type = backup_type FROM @backups WHERE seq = @seq;
            -- URL backups use the credential named after the container (B4).
            SET @from = CASE WHEN @device LIKE N'https://%' THEN N'URL = ' ELSE N'DISK = ' END
                      + dbo.fn_sql_literal(@device);
            -- NORECOVERY on every step: the database waits for the next one.
            SET @sql = CASE WHEN @type = 'L' THEN N'RESTORE LOG ' ELSE N'RESTORE DATABASE ' END
                     + QUOTENAME(@restore_database) + N' FROM ' + @from
                     + N' WITH NORECOVERY, CHECKSUM'
                     + CASE WHEN @type = 'D' THEN N', ' + @moves ELSE N'' END + N';';
            EXEC sys.sp_executesql @sql;
            SET @seq += 1;
        END;

        -- Bring it online (roll back unfinished transactions).
        SET @sql = N'RESTORE DATABASE ' + QUOTENAME(@restore_database) + N' WITH RECOVERY;';
        EXEC sys.sp_executesql @sql;

        -- -------------------------------------------------------------------
        -- 3. Integrity check of the restored copy. Any corruption raises an
        --    error and lands in the CATCH below.
        -- -------------------------------------------------------------------
        SET @checkdb_started = SYSUTCDATETIME();
        SET @sql = N'DBCC CHECKDB (' + QUOTENAME(@restore_database) + N') WITH NO_INFOMSGS, ALL_ERRORMSGS;';
        EXEC sys.sp_executesql @sql;

        -- -------------------------------------------------------------------
        -- 4. Record, then drop the copy.
        -- -------------------------------------------------------------------
        UPDATE dbo.restore_test_runs
        SET finished_at = SYSUTCDATETIME(), status = 'succeeded',
            restore_seconds = DATEDIFF(SECOND, @restore_started, @checkdb_started),
            checkdb_seconds = DATEDIFF(SECOND, @checkdb_started, SYSUTCDATETIME())
        WHERE run_id = @run_id;

        SET @sql = N'DROP DATABASE ' + QUOTENAME(@restore_database) + N';';
        EXEC sys.sp_executesql @sql;

        SELECT run_id, status, full_backup_finished_at, log_backups_restored, restored_to,
               restore_seconds, checkdb_seconds
        FROM dbo.restore_test_runs WHERE run_id = @run_id;
    END TRY
    BEGIN CATCH
        DECLARE @error NVARCHAR(2000) = LEFT(ERROR_MESSAGE(), 2000);

        UPDATE dbo.restore_test_runs
        SET finished_at = SYSUTCDATETIME(), status = 'failed', error_message = @error,
            restore_seconds = CASE WHEN @restore_started IS NOT NULL
                                   THEN DATEDIFF(SECOND, @restore_started, COALESCE(@checkdb_started, SYSUTCDATETIME())) END
        WHERE run_id = @run_id;

        -- Free the disk space even after a failure; the evidence is in the row.
        IF DB_ID(@restore_database) IS NOT NULL
        BEGIN
            BEGIN TRY
                SET @sql = N'ALTER DATABASE ' + QUOTENAME(@restore_database) + N' SET SINGLE_USER WITH ROLLBACK IMMEDIATE; '
                         + N'DROP DATABASE ' + QUOTENAME(@restore_database) + N';';
                EXEC sys.sp_executesql @sql;
            END TRY
            BEGIN CATCH
                -- A database still restoring can't be set to SINGLE_USER; drop it directly.
                SET @sql = N'DROP DATABASE ' + QUOTENAME(@restore_database) + N';';
                EXEC sys.sp_executesql @sql;
            END CATCH;
        END;

        THROW;                              -- the job must fail visibly
    END CATCH;
END;
GO
