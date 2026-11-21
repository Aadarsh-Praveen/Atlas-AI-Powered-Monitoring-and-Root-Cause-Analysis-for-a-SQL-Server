<#
.SYNOPSIS
    Deploys the latest ShopLite API to prod-sql-01: pull, install, set the
    version, restart, wait for /health, print the running version.

.DESCRIPTION
    Run on the VM, in PowerShell as Administrator, for every release:

        C:\atlas\shoplite\api\deploy\deploy.ps1 -Version 1.0.1

    Steps:
      1. Checks: the repo, the API's .env, and the shoplite-api service exist.
      2. Pulls the branch from GitHub (fast-forward only). Refuses if the VM's
         copy has local changes, so production always matches GitHub.
      3. Installs requirements.txt into shoplite\api\.venv (creates it if
         missing). Already-installed packages are skipped quickly.
      4. Sets APP_VERSION in shoplite\api\.env to -Version. The version shows
         in the X-App-Version header, in /health, and in SQL Server's session
         tag (ShopLite-API/<version>), so Atlas can tell releases apart.
      5. If the Caddyfile changed in this pull, validates it and restarts Caddy.
      6. Restarts the API through restart-api.ps1: stop the service, kill any
         old API process that survived and make sure port 8000 is free
         (reporting what it found), start it, and poll /health until it
         returns status "ok" with the NEW version (or give up after
         -HealthTimeoutSeconds and print the end of the error log).

    Exit code 0 on success, 1 on failure.

    Rolling back: check out the previous commit and redeploy it with a new
    version number (see README.md, "Rolling back").

.PARAMETER Version
    The new version, MAJOR.MINOR.PATCH (e.g. 1.0.1). Must change with each
    deploy.

.PARAMETER RepoPath
    Where the repo is cloned. Default C:\atlas.

.PARAMETER Branch
    Branch to deploy. Default main.

.PARAMETER SkipPull
    Deploy the code already on disk (for a rollback, or to only change the
    version or restart).

.PARAMETER HealthTimeoutSeconds
    How long to wait for a healthy API after the restart. Default 60.

.EXAMPLE
    .\deploy.ps1 -Version 1.0.1
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,
    [string]$RepoPath = 'C:\atlas',
    [string]$Branch = 'main',
    [switch]$SkipPull,
    [int]$HealthTimeoutSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$apiDir        = Join-Path $RepoPath 'shoplite\api'
$envFile       = Join-Path $apiDir '.env'
$venvPython    = Join-Path $apiDir '.venv\Scripts\python.exe'
$requirements  = Join-Path $apiDir 'requirements.txt'
$caddyfile     = Join-Path $apiDir 'deploy\Caddyfile'
$caddyExe      = 'C:\services\caddy\caddy.exe'
$apiService    = 'shoplite-api'
$caddyService  = 'caddy'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "[OK]   $Message" -ForegroundColor Green }
function Stop-Deploy([string]$Message) {
    Write-Host "`n[FAIL] $Message" -ForegroundColor Red
    exit 1
}

# Run git in the repo; stop the deploy if it fails. Returns its output lines.
# Run a native program (git, py, pip, caddy) and return its output as text
# lines; stop the deploy, with that output, if it fails.
#
# Why this wrapper: in Windows PowerShell 5.1, when a native program's stderr
# is redirected (2>&1), EVERY stderr line becomes an error record, and with
# $ErrorActionPreference = 'Stop' (top of this script) the first one stops the
# script, even when the program succeeds. git writes normal progress to stderr
# ("From https://github.com/..."), and Caddy writes its log lines there. So:
# relax the preference to 'Continue' for this one call, turn every line (stdout
# or stderr) into plain text, and judge success ONLY by the exit code.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$What)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $Exe @Arguments 2>&1 | ForEach-Object { "$_" })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) {
        Stop-Deploy "$What failed (exit code $exitCode):`n$($output -join "`n")"
    }
    $output
}

# git in the repo, through Invoke-Native. PowerShell unwraps a single line into
# a plain [string], so callers wrap the call in @(...) before indexing;
# otherwise [0] would be the first character.
function Invoke-Git {
    Invoke-Native -Exe 'git' -Arguments (@('-C', $RepoPath) + @($args)) -What "git $($args -join ' ')"
}

# -----------------------------------------------------------------------------
# 1. Checks
# -----------------------------------------------------------------------------
Write-Step "Checks"
if (-not (Test-Path -LiteralPath (Join-Path $RepoPath '.git'))) { Stop-Deploy "No Git repo at $RepoPath." }
if (-not (Test-Path -LiteralPath $envFile)) { Stop-Deploy "No $envFile. Create it from deploy\.env.production.example (README.md)." }
if (-not (Get-Service -Name $apiService -ErrorAction SilentlyContinue)) {
    Stop-Deploy "The $apiService service isn't installed. Follow README.md, First-time install."
}
Write-Ok "Repo, .env, and service found."

# -----------------------------------------------------------------------------
# 2. Pull
# -----------------------------------------------------------------------------
Write-Step "Code"
$before = @(Invoke-Git rev-parse --short HEAD)[0]
$changedFiles = @()

if ($SkipPull) {
    Write-Ok "Skipping pull; deploying commit $before as it is on disk."
}
else {
    # Production must match GitHub exactly: no edits made directly on the VM.
    $localChanges = @(Invoke-Git status --porcelain | Where-Object { $_ -ne '' })
    if ($localChanges.Count -gt 0) {
        Stop-Deploy ("The VM's copy has local changes. Commit them from your Mac instead, or discard them " +
                     "(git -C $RepoPath checkout -- .):`n$($localChanges -join "`n")")
    }
    Invoke-Git fetch origin $Branch | Out-Null
    Invoke-Git checkout $Branch | Out-Null
    # --ff-only: never create a merge commit on the server.
    Invoke-Git pull --ff-only origin $Branch | Out-Null
    $after = @(Invoke-Git rev-parse --short HEAD)[0]
    if ($after -eq $before) {
        Write-Ok "Already up to date at $after."
    }
    else {
        $changedFiles = @(Invoke-Git diff --name-only $before $after)
        Write-Ok "Updated $before -> $after ($($changedFiles.Count) files changed)."
    }
}
$commit = @(Invoke-Git rev-parse --short HEAD)[0]

if ($changedFiles -contains 'shoplite/api/deploy/shoplite-api.xml' -or $changedFiles -contains 'shoplite/api/deploy/caddy.xml') {
    Write-Warning "A service definition (.xml) changed. Those changes apply only after reinstalling the service; see README.md, 'Changing a service definition'."
}

# -----------------------------------------------------------------------------
# 3. Requirements
# -----------------------------------------------------------------------------
Write-Step "Requirements"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating the virtual environment shoplite\api\.venv..."
    $null = Invoke-Native -Exe 'py' -Arguments @('-3.12', '-m', 'venv', (Join-Path $apiDir '.venv')) `
                          -What "Creating the virtual environment with 'py -3.12'"
}
# pip prints warnings to stderr even on success; only its exit code counts.
$null = Invoke-Native -Exe $venvPython `
                      -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-r', $requirements) `
                      -What "pip install (the service wasn't restarted, so the previous version is still running)"
Write-Ok "Requirements installed."

# -----------------------------------------------------------------------------
# 4. Version
# -----------------------------------------------------------------------------
Write-Step "Version"
$lines = @(Get-Content -LiteralPath $envFile)
$previous = ($lines | Where-Object { $_ -match '^\s*APP_VERSION\s*=' } | Select-Object -First 1)
$previousVersion = if ($previous) { ($previous -split '=', 2)[1].Trim() } else { '(not set)' }

if ($previousVersion -eq $Version) {
    Write-Warning "APP_VERSION is already $Version. Each deploy should get a new version, so releases can be told apart."
}
if ($previous) {
    $lines = @($lines | ForEach-Object { if ($_ -match '^\s*APP_VERSION\s*=') { "APP_VERSION=$Version" } else { $_ } })
}
else {
    $lines += "APP_VERSION=$Version"
}
# UTF-8 WITHOUT a byte-order mark (Windows PowerShell's -Encoding UTF8 adds
# one, which would corrupt the first setting's name). Writing into the
# existing file keeps its restricted permissions.
[System.IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
Write-Ok "APP_VERSION $previousVersion -> $Version"

# -----------------------------------------------------------------------------
# 5. Restart
# -----------------------------------------------------------------------------
Write-Step "Restart"
if ($changedFiles -contains 'shoplite/api/deploy/Caddyfile') {
    # Caddy logs to stderr even when the file is valid; only the exit code counts.
    $null = Invoke-Native -Exe $caddyExe -Arguments @('validate', '--config', $caddyfile, '--adapter', 'caddyfile') `
                          -What "Validating the new Caddyfile (Caddy and the API were NOT restarted)"
    Restart-Service -Name $caddyService
    Write-Ok "Caddyfile changed: Caddy restarted."
}
# The API restart goes through restart-api.ps1: it stops the service, kills
# any API process that survived the stop and frees port 8000 (reporting what
# it did), starts the service, and waits for /health with the new version.
$restartScript = Join-Path $PSScriptRoot 'restart-api.ps1'
& $restartScript -ExpectedVersion $Version -HealthTimeoutSeconds $HealthTimeoutSeconds
if ($LASTEXITCODE -ne 0) {
    Stop-Deploy "The API didn't come back healthy on version $Version (details above). See README.md, 'Rolling back'."
}

Write-Host "`nDeployed ShopLite API $Version (commit $commit)." -ForegroundColor Green
Write-Host "Check from your Mac: https://shoplite-aadarsh.northcentralus.cloudapp.azure.com/health"
exit 0
