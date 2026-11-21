<#
.SYNOPSIS
    Configures SQL Server on prod-sql-01: max server memory, default data and
    log paths on the data disk, and checks that SQL authentication is on.

.DESCRIPTION
    Run after 01-prepare-data-disk.ps1. Connects to the local SQL Server with
    Windows authentication (the VM's administrator account is a SQL sysadmin
    on the marketplace image) and:

      1. Max server memory -> 5120 MB (5 GB). By default SQL Server will take
         nearly all 8 GB of RAM, starving Windows, the API, and Caddy. 5 GB
         leaves about 3 GB for them. Takes effect immediately, no restart.

      2. Default data and log paths -> F:\SQLData and F:\SQLLog, so SalesDB
         (created later by apply_schema.py) lands on the data disk, not C:.
         These are registry settings that SQL Server reads at startup, so the
         service is restarted ONLY if they had to change.

      3. Checks that SQL authentication (mixed mode) is on. The SQL IaaS
         extension enables it (infra/modules/sql-vm.bicep); the API and the
         database tools both log in with SQL logins. With -SqlAdminUser, also
         checks that your SQL admin login exists, is enabled, and is sysadmin.

    Safe to re-run: each setting is read first and changed only if different.

.PARAMETER MaxServerMemoryMB
    Cap on SQL Server's memory in MB. Default 5120 for the 8 GB B2as_v2.

.PARAMETER DataPath
    Default folder for new data files. Default F:\SQLData (from script 01).

.PARAMETER LogPath
    Default folder for new log files. Default F:\SQLLog (from script 01).

.PARAMETER SqlAdminUser
    Optional. The SQL admin login name you chose in Bicep (sqlAdminUsername),
    to verify it exists and is sysadmin.

.EXAMPLE
    .\02-configure-sql.ps1 -SqlAdminUser shoplite_admin
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [int]$MaxServerMemoryMB = 5120,
    [string]$DataPath = 'F:\SQLData',
    [string]$LogPath = 'F:\SQLLog',
    [string]$SqlAdminUser = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "[OK]   $Message" -ForegroundColor Green }
function Write-Skip([string]$Message) { Write-Host "[SKIP] $Message" -ForegroundColor DarkGray }

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
if (-not (Get-Command sqlcmd -ErrorAction SilentlyContinue)) {
    throw "sqlcmd wasn't found on PATH. It ships with the SQL Server image; open a new PowerShell window, or check 'C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\*\Tools\Binn'."
}

# Run one query against the local default instance with Windows auth.
#   -E  Windows authentication     -b  exit code 1 on any SQL error
#   -h -1 -W  no headers, trimmed values (easy to read back into PowerShell)
#   -s "|"    columns separated by "|", so multi-column rows split reliably
#
# Returns the non-empty output lines, one string per row.
# IMPORTANT: PowerShell unwraps a function's output. One line comes back as a
# plain [string], not an array, and then $result[0] is its first CHARACTER and
# $result.Count fails under Set-StrictMode. So callers always wrap the call,
# @(Invoke-Sql ...), or use Get-SqlValue / Get-SqlInt below.
function Invoke-Sql([string]$Query) {
    # Windows PowerShell 5.1 turns redirected stderr lines into error records,
    # which would stop the script (ErrorActionPreference 'Stop') before our own
    # message. Relax it for this call; success is judged by the exit code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& sqlcmd -S localhost -E -b -h -1 -W -s '|' -Q "SET NOCOUNT ON; $Query" 2>&1 |
                    ForEach-Object { "$_" })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) {
        throw "sqlcmd failed (exit code $exitCode):`n$($output -join "`n")"
    }
    $output | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
}

# The first row of a query that returns one value, as a string.
function Get-SqlValue([string]$Query) {
    $rows = @(Invoke-Sql $Query)
    if ($rows.Count -eq 0) { throw "The query returned no rows: $Query" }
    return [string]$rows[0]
}

# The same, parsed as a whole number (fails loudly instead of guessing).
function Get-SqlInt([string]$Query) {
    $text = Get-SqlValue $Query
    $number = 0
    if (-not [int]::TryParse($text, [ref]$number)) {
        throw "Expected a number from SQL Server but got '$text': $Query"
    }
    return $number
}

# T-SQL string literal: N'...' with quotes doubled.
function ConvertTo-SqlLiteral([string]$Value) { return "N'" + $Value.Replace("'", "''") + "'" }

function Wait-SqlReady([int]$TimeoutSeconds = 90) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try { Invoke-Sql "SELECT 1;" | Out-Null; return }
        catch { Start-Sleep -Seconds 3 }
    }
    throw "SQL Server didn't accept connections within $TimeoutSeconds seconds after the restart. Check the SQL Server error log."
}

Write-Step "Connecting to SQL Server"
$version = Get-SqlValue "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)) + N' ' + CAST(SERVERPROPERTY('Edition') AS nvarchar(100));"
Write-Ok "Connected: SQL Server $version"

# -----------------------------------------------------------------------------
# 1. Max server memory
# -----------------------------------------------------------------------------
Write-Step "Max server memory"

$totalRamMB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB)
if ($MaxServerMemoryMB -gt ($totalRamMB - 2048)) {
    Write-Warning "Max server memory $MaxServerMemoryMB MB leaves less than 2 GB of the VM's $totalRamMB MB for Windows, the API, and Caddy."
}

$currentMemoryMB = Get-SqlInt "SELECT CAST(value_in_use AS int) FROM sys.configurations WHERE name = N'max server memory (MB)';"
if ($currentMemoryMB -eq $MaxServerMemoryMB) {
    Write-Skip "Already $MaxServerMemoryMB MB."
}
else {
    Invoke-Sql @"
EXEC sp_configure N'show advanced options', 1; RECONFIGURE;
EXEC sp_configure N'max server memory (MB)', $MaxServerMemoryMB; RECONFIGURE;
"@ | Out-Null
    Write-Ok "Changed from $currentMemoryMB MB to $MaxServerMemoryMB MB (effective now)."
}

# -----------------------------------------------------------------------------
# 2. Default data and log paths
# -----------------------------------------------------------------------------
Write-Step "Default data and log paths"

foreach ($path in @($DataPath, $LogPath)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "$path doesn't exist. Run .\01-prepare-data-disk.ps1 first."
    }
}

function Get-DefaultPaths {
    $rows = @(Invoke-Sql "SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(260)); SELECT CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(260));")
    if ($rows.Count -ne 2) { throw "Expected 2 rows (data path, log path) but got $($rows.Count): $($rows -join ' / ')" }
    return @{ Data = ([string]$rows[0]).TrimEnd('\'); Log = ([string]$rows[1]).TrimEnd('\') }
}

$current = Get-DefaultPaths
$wantData = $DataPath.TrimEnd('\')
$wantLog  = $LogPath.TrimEnd('\')

if ($current.Data -eq $wantData -and $current.Log -eq $wantLog) {
    Write-Skip "Already data = $wantData, log = $wantLog."
}
else {
    Write-Host "Currently data = $($current.Data), log = $($current.Log). Updating..."
    # xp_instance_regwrite writes this instance's registry keys (the same
    # setting as Server Properties > Database Settings in SSMS).
    Invoke-Sql @"
EXEC master.dbo.xp_instance_regwrite N'HKEY_LOCAL_MACHINE', N'Software\Microsoft\MSSQLServer\MSSQLServer', N'DefaultData', REG_SZ, $(ConvertTo-SqlLiteral $wantData);
EXEC master.dbo.xp_instance_regwrite N'HKEY_LOCAL_MACHINE', N'Software\Microsoft\MSSQLServer\MSSQLServer', N'DefaultLog',  REG_SZ, $(ConvertTo-SqlLiteral $wantLog);
"@ | Out-Null

    # The new paths are read at startup: restart SQL Server (and SQL Agent,
    # which stops with it).
    $agent = Get-Service -Name 'SQLSERVERAGENT' -ErrorAction SilentlyContinue
    $agentWasRunning = $agent -and $agent.Status -eq 'Running'
    Write-Host "Restarting SQL Server to apply the paths..."
    Restart-Service -Name 'MSSQLSERVER' -Force
    if ($agentWasRunning) { Start-Service -Name 'SQLSERVERAGENT' }
    Wait-SqlReady

    $current = Get-DefaultPaths
    if ($current.Data -ne $wantData -or $current.Log -ne $wantLog) {
        throw "After the restart, the paths are data = $($current.Data), log = $($current.Log); expected $wantData and $wantLog."
    }
    Write-Ok "Data = $wantData, log = $wantLog (SQL Server restarted)."
}

# -----------------------------------------------------------------------------
# 3. SQL authentication
# -----------------------------------------------------------------------------
Write-Step "SQL authentication"

$integratedOnly = Get-SqlInt "SELECT CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int);"
if ($integratedOnly -eq 1) {
    throw ("SQL Server is in Windows-authentication-only mode, so the API and the database tools can't log in. " +
           "In the portal: SQL virtual machine prod-sql-01 > Security configuration > enable SQL Authentication, then re-run.")
}
Write-Ok "Mixed mode: SQL logins are allowed."

if ($SqlAdminUser) {
    $name = ConvertTo-SqlLiteral $SqlAdminUser
    # @(...) keeps a single row as a one-item array (see Invoke-Sql).
    $rows = @(Invoke-Sql "SELECT CAST(is_disabled AS int), IS_SRVROLEMEMBER(N'sysadmin', name) FROM sys.server_principals WHERE name = $name AND type = 'S';")
    if ($rows.Count -eq 0) {
        throw "SQL login '$SqlAdminUser' doesn't exist. Check sqlAdminUsername in infra/prod.parameters.local.json."
    }
    # One row, two columns separated by "|": is_disabled|is_sysadmin
    $parts = @(([string]$rows[0]).Split('|') | ForEach-Object { $_.Trim() })
    if ($parts.Count -ne 2) { throw "Unexpected result for login '$SqlAdminUser': $($rows[0])" }
    if ($parts[0] -ne '0') { throw "SQL login '$SqlAdminUser' exists but is disabled." }
    if ($parts[1] -ne '1') { throw "SQL login '$SqlAdminUser' exists but isn't a sysadmin." }
    Write-Ok "SQL admin login '$SqlAdminUser' exists, is enabled, and is sysadmin."
}
else {
    Write-Skip "No -SqlAdminUser given; not checking the admin login."
}

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Step "Done"
Write-Host "Max server memory: $MaxServerMemoryMB MB   Data: $wantData   Log: $wantLog"
Write-Host "Next: .\03-check-prerequisites.ps1"
