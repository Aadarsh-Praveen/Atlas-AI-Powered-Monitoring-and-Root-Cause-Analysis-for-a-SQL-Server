<#
.SYNOPSIS
    Creates (or replaces) the SQL Server credential that lets prod-sql-01
    write backups to the Blob container, using a SAS token you paste in.

.DESCRIPTION
    BACKUP ... TO URL finds its credential by name: a credential named
    exactly the container URL, with IDENTITY 'SHARED ACCESS SIGNATURE' and
    the SAS token as its SECRET. This script:

      1. Asks for the container URL (the Bicep output backupContainerUrl)
         and checks its shape.
      2. Asks for the SAS token at a hidden prompt. It's never written to a
         file, never put on a command line, and never printed.
      3. Checks the SAS before using it:
           - container-level (sr=c), not an account-wide SAS;
           - HTTPS only (spr=https);
           - permissions include read, write, create, list (r w c l):
             write/create/list to back up, read to RESTORE from the URL
             (the weekly restore test);
           - NO delete (d): the lifecycle rule deletes old backups, so a
             leaked SAS can't be used to wipe them;
           - expiry (se) in the future; warns if it's within 30 days.
      4. Creates the credential, or replaces its secret if it exists
         (that's also how to rotate the SAS before it expires). The SAS
         goes to SQL Server as a query PARAMETER over a local Windows-
         authenticated connection.
      5. Tests it: a COPY_ONLY backup of the tiny model database to
         <container>/credential-test/, then RESTORE HEADERONLY from it
         (proves write and read). The lifecycle rule removes the test file.
      6. Prints the SAS expiry date: record it in the runbook.

    Run on prod-sql-01, in PowerShell as Administrator. Safe to re-run.

.PARAMETER ContainerUrl
    https://<account>.blob.core.windows.net/sqlbackups (asked for if omitted).

.PARAMETER SkipTest
    Create the credential without the test backup.

.EXAMPLE
    .\01-create-backup-credential.ps1 -ContainerUrl https://stshopliteabc123.blob.core.windows.net/sqlbackups
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$ContainerUrl = '',
    [switch]$SkipTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "[OK]   $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }

# -----------------------------------------------------------------------------
# SQL Server through .NET's SqlClient (built into Windows PowerShell 5.1),
# not sqlcmd: it can send the SAS as a typed parameter, so the secret never
# appears in a process command line, a script file, or shell history.
# Windows authentication: the VM administrator is a SQL sysadmin.
# -----------------------------------------------------------------------------
function Invoke-SqlCommand([string]$Query, [hashtable]$Parameters = @{}, [int]$TimeoutSeconds = 300) {
    $connection = New-Object System.Data.SqlClient.SqlConnection 'Server=localhost;Database=master;Integrated Security=SSPI;Application Name=ShopLite-backup-setup'
    try {
        $connection.Open()
        $command = $connection.CreateCommand()
        $command.CommandText = $Query
        $command.CommandTimeout = $TimeoutSeconds
        foreach ($name in $Parameters.Keys) {
            [void]$command.Parameters.Add((New-Object System.Data.SqlClient.SqlParameter($name, [string]$Parameters[$name])))
        }
        # @(): always an array, even for one row (PowerShell 5.1 unwraps single items).
        $table = New-Object System.Data.DataTable
        $reader = $command.ExecuteReader()
        if ($reader.FieldCount -gt 0) { $table.Load($reader) } else { $reader.Close() }   # BACKUP returns no rows
        return , @($table.Rows)
    }
    finally {
        $connection.Dispose()
    }
}

# -----------------------------------------------------------------------------
# 1. Container URL
# -----------------------------------------------------------------------------
Write-Step 'Container URL'
if (-not $ContainerUrl) {
    $ContainerUrl = Read-Host 'Container URL (Bicep output backupContainerUrl)'
}
$ContainerUrl = $ContainerUrl.Trim().TrimEnd('/')
# Storage account: 3-24 lowercase letters/digits; container: 3-63 lowercase, digits, hyphens.
if ($ContainerUrl -notmatch '^https://[a-z0-9]{3,24}\.blob\.core\.windows\.net/[a-z0-9-]{3,63}$') {
    throw "That doesn't look like a container URL: expected https://<account>.blob.core.windows.net/<container>, with no SAS on the end."
}
Write-Ok $ContainerUrl

# -----------------------------------------------------------------------------
# 2-3. SAS token: hidden prompt, then checks
# -----------------------------------------------------------------------------
Write-Step 'SAS token'
$secure = Read-Host 'Paste the SAS token (input hidden)' -AsSecureString
$sas = [System.Net.NetworkCredential]::new('', $secure).Password
$sas = $sas.Trim().TrimStart('?')          # SQL Server wants it without the leading "?"
if (-not $sas) { throw 'No SAS token entered.' }

$fields = @{}
foreach ($pair in $sas.Split('&')) {
    $parts = $pair.Split('=', 2)
    if ($parts.Count -eq 2) { $fields[$parts[0]] = [System.Uri]::UnescapeDataString($parts[1]) }
}

$problems = @()
if (-not $fields.ContainsKey('sig')) { $problems += 'no signature (sig): paste the whole token' }
if ($fields.ContainsKey('ss') -or $fields.ContainsKey('srt')) {
    $problems += 'this is an ACCOUNT SAS (whole storage account); create one on the container instead'
}
elseif (-not $fields.ContainsKey('sr') -or $fields['sr'] -ne 'c') {
    $problems += "signed resource must be the container (sr=c), found '$($fields['sr'])'"
}
if (-not $fields.ContainsKey('spr') -or $fields['spr'] -ne 'https') {
    $problems += 'allowed protocols must be HTTPS only (spr=https)'
}
$permissions = if ($fields.ContainsKey('sp')) { $fields['sp'] } else { '' }
foreach ($needed in 'r', 'w', 'c', 'l') {
    if (-not $permissions.Contains($needed)) { $problems += "missing permission '$needed' (need read, write, create, list)" }
}
if ($permissions.Contains('d')) { $problems += "has Delete ('d'): create it without Delete (the lifecycle rule deletes old backups)" }

$expiry = $null
if ($fields.ContainsKey('se')) {
    $expiry = [datetime]::Parse($fields['se'], [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal)
    if ($expiry -le [datetime]::UtcNow) { $problems += "it already expired ($($expiry.ToString('yyyy-MM-dd HH:mm')) UTC)" }
}
else {
    $problems += 'no expiry (se)'
}

if ($problems.Count -gt 0) {
    foreach ($p in $problems) { Write-Host "[FAIL] $p" -ForegroundColor Red }
    throw 'The SAS token was refused; nothing was changed. See server/backups/README.md, "Create the SAS".'
}
Write-Ok "Container SAS, HTTPS only, permissions '$permissions', expires $($expiry.ToString('yyyy-MM-dd HH:mm')) UTC"
if ($expiry -lt [datetime]::UtcNow.AddDays(30)) {
    Write-Warn 'It expires within 30 days: backups will start failing then. Consider a 12-month SAS.'
}

# -----------------------------------------------------------------------------
# 4. Create or replace the credential
# -----------------------------------------------------------------------------
# CREATE/ALTER CREDENTIAL don't accept parameters for their name or secret,
# so the statement is built INSIDE SQL Server from the parameters:
# QUOTENAME for the name (max 128 characters: the URL fits) and quotes
# doubled for the secret (a SAS is longer than QUOTENAME's limit).
Write-Step 'SQL Server credential'
$credentialSql = @'
DECLARE @verb NVARCHAR(10) = CASE WHEN EXISTS (SELECT 1 FROM sys.credentials WHERE name = @name)
                                  THEN N'ALTER' ELSE N'CREATE' END;
DECLARE @statement NVARCHAR(MAX) =
    @verb + N' CREDENTIAL ' + QUOTENAME(@name)
    + N' WITH IDENTITY = ''SHARED ACCESS SIGNATURE'', SECRET = N'''
    + REPLACE(@secret, N'''', N'''''') + N''';';
EXEC sys.sp_executesql @statement;
SELECT @verb AS action;
'@
$result = Invoke-SqlCommand $credentialSql @{ '@name' = $ContainerUrl; '@secret' = $sas }
$sas = $null; $secure = $null                # drop the secret from memory as soon as it's stored
Write-Ok ("Credential {0}D: [{1}]" -f $result[0]['action'], $ContainerUrl)

# -----------------------------------------------------------------------------
# 5. Test: write a small backup, read its header back
# -----------------------------------------------------------------------------
if ($SkipTest) {
    Write-Warn 'Test skipped (-SkipTest).'
}
else {
    Write-Step 'Test backup (model database, COPY_ONLY: doesn''t touch any backup chain)'
    $testUrl = '{0}/credential-test/model_{1}.bak' -f $ContainerUrl, [datetime]::UtcNow.ToString('yyyyMMdd_HHmmss')
    try {
        # BACKUP and RESTORE accept a variable for the URL, so a plain parameter works here.
        Invoke-SqlCommand 'BACKUP DATABASE model TO URL = @url WITH COPY_ONLY, COMPRESSION, CHECKSUM;' @{ '@url' = $testUrl } | Out-Null
        Write-Ok "Wrote $testUrl"
        $header = Invoke-SqlCommand 'RESTORE HEADERONLY FROM URL = @url;' @{ '@url' = $testUrl }
        Write-Ok ("Read it back: database {0}, backup finished {1}" -f $header[0]['DatabaseName'], $header[0]['BackupFinishDate'])
    }
    catch {
        Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red
        Write-Host '       Common causes: the SAS is for another container or storage account, it was'
        Write-Host '       created without Read/Write/Create, or the VM has no outbound internet (port 443).'
        throw 'The credential exists but the test backup failed.'
    }
}

# -----------------------------------------------------------------------------
# 6. Expiry: record it
# -----------------------------------------------------------------------------
Write-Step 'Done'
Write-Host ("Record in knowledge/runbooks/shoplite-production.md: backup SAS expires {0} UTC." -f $expiry.ToString('yyyy-MM-dd'))
Write-Host 'Rotate it before then: create a new SAS and run this script again (it replaces the secret).'
