<#
.SYNOPSIS
    Checks that prod-sql-01 has what ShopLite needs: Python 3.12, Git, ODBC
    Driver 18 for SQL Server, and the UTC time zone.

.DESCRIPTION
    Read-only: installs and changes nothing. For each missing piece it prints
    where to download it. Install what's missing, open a NEW PowerShell window
    (so PATH changes are picked up), and run it again until every line is OK.

      Python 3.12   runs the ShopLite API and the database tools
                    (apply_schema.py, seed.py). Must be 3.12, the version the
                    project uses locally, and installed for ALL USERS: the
                    API service's account (LocalService) can't read a
                    per-user install inside an admin's profile.
      Git           clones and updates the repo (deploy.ps1 pulls with it).
      ODBC Driver 18 for SQL Server
                    pyodbc's driver for SQL Server; the API's connection
                    string names it exactly.
      Time zone UTC set by Bicep (infra/modules/vm.bicep); checked here
                    because every log is expected to be in UTC.

    Exit code 0 when everything is OK, 1 when anything is missing, so it can
    also be used from other scripts.

.EXAMPLE
    .\03-check-prerequisites.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:missing = 0

function Write-Pass([string]$Name, [string]$Detail) {
    Write-Host ("[OK]      {0,-16} {1}" -f $Name, $Detail) -ForegroundColor Green
}
function Write-Fail([string]$Name, [string]$Detail, [string]$Fix) {
    $script:missing++
    Write-Host ("[MISSING] {0,-16} {1}" -f $Name, $Detail) -ForegroundColor Red
    Write-Host ("          {0,-16} {1}" -f '', $Fix) -ForegroundColor Yellow
}

# Run a command and return its first line of output, or $null if it fails.
function Get-CommandOutput([string]$Command, [string[]]$Arguments) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) { return $null }
    # Some tools print their version (or harmless notes) to stderr; in Windows
    # PowerShell 5.1 that would raise an error here. Relax the preference for
    # this call and judge success by the exit code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $Command @Arguments 2>&1 | ForEach-Object { "$_" })
        if ($LASTEXITCODE -ne 0) { return $null }
        return ("$($output | Select-Object -First 1)").Trim()
    }
    catch { return $null }
    finally { $ErrorActionPreference = $previous }
}

Write-Host "`nChecking prerequisites on $env:COMPUTERNAME`n" -ForegroundColor Cyan

# -----------------------------------------------------------------------------
# Python 3.12
# -----------------------------------------------------------------------------
# The Windows "py" launcher can pick a specific version even when several are
# installed; fall back to "python" on PATH.
$pythonVersion = Get-CommandOutput 'py' @('-3.12', '--version')
if (-not $pythonVersion) { $pythonVersion = Get-CommandOutput 'python' @('--version') }

if ($pythonVersion -and $pythonVersion -match 'Python 3\.12\.') {
    # Where it's installed matters: the API service runs as LocalService,
    # which can't read a per-user install inside an admin's profile.
    $pythonHome = Get-CommandOutput 'py' @('-3.12', '-c', 'import sys; print(sys.base_prefix)')
    if ($pythonHome -and $pythonHome -like "$env:SystemDrive\Users\*") {
        Write-Fail 'Python 3.12' "$pythonVersion is installed for one user only ($pythonHome)." `
            'Uninstall it, then reinstall with Customize installation > "Install Python 3.12 for all users".'
    }
    else {
        Write-Pass 'Python 3.12' "$pythonVersion ($pythonHome)"
    }
}
elseif ($pythonVersion) {
    Write-Fail 'Python 3.12' "Found $pythonVersion, but 3.12 is required." `
        'Install Python 3.12 (64-bit) from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH".'
}
else {
    Write-Fail 'Python 3.12' 'Not found.' `
        'Install Python 3.12 (64-bit) from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH".'
}

# -----------------------------------------------------------------------------
# Git
# -----------------------------------------------------------------------------
$gitVersion = Get-CommandOutput 'git' @('--version')
if ($gitVersion) {
    Write-Pass 'Git' $gitVersion
}
else {
    Write-Fail 'Git' 'Not found.' 'Install Git for Windows from https://git-scm.com/download/win (default options are fine).'
}

# -----------------------------------------------------------------------------
# ODBC Driver 18 for SQL Server (64-bit, to match 64-bit Python)
# -----------------------------------------------------------------------------
$odbc = Get-OdbcDriver -Name 'ODBC Driver 18 for SQL Server' -Platform '64-bit' -ErrorAction SilentlyContinue
if ($odbc) {
    Write-Pass 'ODBC Driver 18' 'ODBC Driver 18 for SQL Server (64-bit)'
}
else {
    Write-Fail 'ODBC Driver 18' 'Not found (64-bit).' `
        'Install it from https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server (x64 installer).'
}

# -----------------------------------------------------------------------------
# Time zone
# -----------------------------------------------------------------------------
$timeZone = Get-TimeZone
if ($timeZone.Id -eq 'UTC') {
    Write-Pass 'Time zone' "$($timeZone.Id) ($($timeZone.DisplayName))"
}
else {
    Write-Fail 'Time zone' "Is $($timeZone.Id), expected UTC." `
        'Bicep sets UTC at creation. To fix it here: Set-TimeZone -Id UTC (then restart SQL Server so its logs follow).'
}

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Host ''
if ($script:missing -eq 0) {
    Write-Host 'All prerequisites are in place. Next: .\04-configure-firewall.ps1' -ForegroundColor Green
    exit 0
}
else {
    Write-Host "$($script:missing) missing. Install them, open a NEW PowerShell window, and run this again." -ForegroundColor Red
    exit 1
}
