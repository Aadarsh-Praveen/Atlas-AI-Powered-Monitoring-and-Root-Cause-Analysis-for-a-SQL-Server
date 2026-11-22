/*
    server/backups/02_full_recovery_and_first_backup.sql
    Switches SalesDB to FULL recovery and takes the first full backup, which
    starts the log chain. Run once per environment, as the SQL admin, in a
    quiet moment (production: the Phase 4 maintenance window, traffic off).

    Two sqlcmd variables choose where the backup goes:

      Production (Blob Storage; needs 01-create-backup-credential.ps1 first):
        sqlcmd -S localhost -E -I -b -v BACKUP_TARGET=URL
               BACKUP_ROOT=https://<account>.blob.core.windows.net/sqlbackups
               -i server\backups\02_full_recovery_and_first_backup.sql

      Local (a folder inside the container):
        ... -v BACKUP_TARGET=DISK BACKUP_ROOT=/var/opt/mssql/backup ...

    Why FULL recovery: in SIMPLE recovery SQL Server reuses the transaction
    log as soon as it can, so you can only restore to the moment of a full
    or differential backup. In FULL recovery it keeps every change in the
    log until a LOG backup copies it out; the chain of log backups lets you
    restore to any point in time (Phase 4's RPO: about 15 minutes).

    Why the full backup immediately: right after the switch, SalesDB still
    behaves as if it were SIMPLE ("pseudo-simple") until a full backup gives
    the log chain its starting point. Until then a log backup would fail,
    and nothing is protected yet. This script does both, in that order.

    Safe to re-run: the switch happens only if needed, and each run writes a
    new, uniquely named backup file (it never overwrites one). Every later
    backup (B5's jobs) continues the same chain.
*/

USE master;
GO
SET NOCOUNT ON;
GO

DECLARE @target NVARCHAR(10)  = UPPER(N'$(BACKUP_TARGET)');
DECLARE @root   NVARCHAR(400) = N'$(BACKUP_ROOT)';
IF RIGHT(@root, 1) = N'/' SET @root = LEFT(@root, LEN(@root) - 1);

-- ---------------------------------------------------------------------------
-- Checks first: change nothing unless everything needed is in place.
-- ---------------------------------------------------------------------------
IF @target NOT IN (N'DISK', N'URL')
BEGIN
    RAISERROR(N'BACKUP_TARGET must be DISK or URL. Nothing was changed.', 16, 1);
    RETURN;
END;
IF DB_ID(N'SalesDB') IS NULL
BEGIN
    RAISERROR(N'SalesDB doesn''t exist on this server. Nothing was changed.', 16, 1);
    RETURN;
END;
IF @target = N'URL' AND @root NOT LIKE N'https://%.blob.core.windows.net/%'
BEGIN
    RAISERROR(N'BACKUP_ROOT must be the container URL, https://<account>.blob.core.windows.net/<container>. Nothing was changed.', 16, 1);
    RETURN;
END;
-- BACKUP ... TO URL looks for a credential named exactly the container URL.
IF @target = N'URL' AND NOT EXISTS (SELECT 1 FROM sys.credentials WHERE name = @root)
BEGIN
    RAISERROR(N'No credential named %s: run server/backups/01-create-backup-credential.ps1 first. Nothing was changed.', 16, 1, @root);
    RETURN;
END;

-- ---------------------------------------------------------------------------
-- 1. FULL recovery
-- ---------------------------------------------------------------------------
IF (SELECT recovery_model_desc FROM sys.databases WHERE name = N'SalesDB') <> N'FULL'
BEGIN
    ALTER DATABASE SalesDB SET RECOVERY FULL;
    PRINT N'SalesDB switched to FULL recovery.';
END
ELSE
    PRINT N'SalesDB is already in FULL recovery.';

-- ---------------------------------------------------------------------------
-- 2. The full backup
--    COMPRESSION: typically a quarter of the size, so less to upload and
--                 store, and usually faster overall.
--    CHECKSUM:    SQL Server verifies every page's checksum while reading
--                 and stores a checksum for the backup, so corruption is
--                 caught now, not at restore time.
--    Unique name: <root>/SalesDB/SalesDB_FULL_<UTC timestamp>.bak.
-- ---------------------------------------------------------------------------
DECLARE @file NVARCHAR(600) =
    @root + N'/SalesDB/SalesDB_FULL_' + FORMAT(SYSUTCDATETIME(), N'yyyyMMdd_HHmmss') + N'.bak';
PRINT CONCAT(N'Backing up to ', @file, N' ...');

IF @target = N'URL'
    -- MAXTRANSFERSIZE + BLOCKSIZE: Microsoft's recommended settings for
    -- backups to block blobs (larger, fewer blocks: faster, and well within
    -- a blob's 50,000-block limit at SalesDB's size).
    BACKUP DATABASE SalesDB TO URL = @file
        WITH COMPRESSION, CHECKSUM, MAXTRANSFERSIZE = 4194304, BLOCKSIZE = 65536, STATS = 10;
ELSE
    BACKUP DATABASE SalesDB TO DISK = @file
        WITH COMPRESSION, CHECKSUM, STATS = 10;

-- ---------------------------------------------------------------------------
-- 3. Verify: the file is complete and readable, and its checksums match.
--    (Not a substitute for a real restore: B5's weekly restore test does that.)
-- ---------------------------------------------------------------------------
IF @target = N'URL'
    RESTORE VERIFYONLY FROM URL = @file WITH CHECKSUM;
ELSE
    RESTORE VERIFYONLY FROM DISK = @file WITH CHECKSUM;

-- ---------------------------------------------------------------------------
-- 4. Proof: the log chain has started (last_log_backup_lsn is set once a full
--    backup exists in FULL recovery; NULL means still pseudo-simple).
-- ---------------------------------------------------------------------------
SELECT d.name, d.recovery_model_desc,
       CASE WHEN rs.last_log_backup_lsn IS NOT NULL THEN 'log chain started'
            ELSE 'NOT started (pseudo-simple)' END AS log_chain
FROM sys.databases AS d
JOIN sys.database_recovery_status AS rs ON rs.database_id = d.database_id
WHERE d.name = N'SalesDB';

SELECT TOP 1 b.backup_finish_date, b.type AS backup_type,      -- D = full database backup
       CAST(b.backup_size / 1048576.0 AS DECIMAL(10,1))            AS size_mb,
       CAST(b.compressed_backup_size / 1048576.0 AS DECIMAL(10,1)) AS compressed_mb,
       b.has_backup_checksums, m.physical_device_name
FROM msdb.dbo.backupset AS b
JOIN msdb.dbo.backupmediafamily AS m ON m.media_set_id = b.media_set_id
WHERE b.database_name = N'SalesDB'
ORDER BY b.backup_finish_date DESC;
GO
