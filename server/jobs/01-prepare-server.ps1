<#
.SYNOPSIS
    Prepares prod-sql-01 for Phase 4's jobs and Extended Events: two folders
    on the data disk and SQL Server Agent starting automatically.

.DESCRIPTION
    Run on prod-sql-01, in PowerShell as Administrator, before the SQL
    scripts in server/jobs/sql/. Safe to re-run: each step checks first.

      1. F:\XE          Extended Events files (the ShopLite_Diagnostics
                        session). SQL Server itself writes them, so its
                        service account (NT SERVICE\MSSQLSERVER) gets Modify.
      2. F:\Logs\jobs   Output of the ETL job's command step. The step runs
                        as the SQL Agent service account
                        (NT SERVICE\SQLSERVERAGENT), which gets Modify here.
                        (F:\Logs itself is from Phase 2.)
      3. SQL Server Agent: startup type Automatic, and started now. On the
         marketplace image it's Manual, so after every VM start no job would
         run, silently: exactly the "scheduled but never ran" failure
         Phase 4 warns about.

    Each service account gets rights on its own folder only: least privilege
    on disk, the same idea as the SQL logins.

.EXAMPLE
    .\01-prepare-server.ps1
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$XePath = 'F:\XE',
    [string]$JobLogPath = 'F:\Logs\jobs'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "[OK]   $Message" -ForegroundColor Green }
function Write-Skip([string]$Message) { Write-Host "[SKIP] $Message" -ForegroundColor DarkGray }

$sqlServiceAccount   = 'NT SERVICE\MSSQLSERVER'
$agentServiceAccount = 'NT SERVICE\SQLSERVERAGENT'

if (-not (Test-Path 'F:\')) {
    throw 'Drive F: (the data disk) is missing. Run server\vm-setup\01-prepare-data-disk.ps1 first.'
}

# A folder, and Modify for one account on it and everything created inside
# (OI = files, CI = subfolders). icacls prints to stdout, so its exit code
# is the only thing that matters.
function Grant-FolderAccess([string]$Path, [string]$Account) {
    if (Test-Path $Path) {
        Write-Skip "$Path exists"
    }
    else {
        New-Item -ItemType Directory -Path $Path | Out-Null
        Write-Ok "Created $Path"
    }
    & icacls.exe $Path /grant "${Account}:(OI)(CI)M" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed to grant $Account Modify on $Path." }
    Write-Ok "$Account can write to $Path"
}

# -----------------------------------------------------------------------------
# 1-2. Folders
# -----------------------------------------------------------------------------
Write-Step 'Extended Events folder'
Grant-FolderAccess $XePath $sqlServiceAccount

Write-Step 'Job output folder'
Grant-FolderAccess $JobLogPath $agentServiceAccount

# -----------------------------------------------------------------------------
# 3. SQL Server Agent: automatic and running
# -----------------------------------------------------------------------------
Write-Step 'SQL Server Agent'
$agent = Get-Service -Name 'SQLSERVERAGENT'
if ($agent.StartType -ne 'Automatic') {
    Set-Service -Name 'SQLSERVERAGENT' -StartupType Automatic
    Write-Ok 'Startup type set to Automatic (starts with the VM every morning)'
}
else {
    Write-Skip 'Startup type is already Automatic'
}
if ((Get-Service -Name 'SQLSERVERAGENT').Status -ne 'Running') {
    Start-Service -Name 'SQLSERVERAGENT'
    Write-Ok 'Started'
}
else {
    Write-Skip 'Already running'
}

Write-Step 'Done'
Write-Host 'Next: the SQL scripts in server\jobs\sql (see server\jobs\README.md).'
