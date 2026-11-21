<#
.SYNOPSIS
    Restarts the ShopLite API service cleanly: stop, make sure NO old API
    process is left and port 8000 is free, start, and wait for /health.

.DESCRIPTION
    Use this instead of Restart-Service after changing shoplite\api\.env (for
    example ALLOWED_ORIGINS). deploy.ps1 calls it too.

        C:\atlas\shoplite\api\deploy\restart-api.ps1

    Why not just Restart-Service: in production, old API processes once
    survived a service stop and kept serving requests on port 8000 with their
    OLD settings, so a config change "didn't take effect". This script checks
    for leftovers and removes them before starting the service again.

    How leftovers are found (by who runs them, not by command line):
      - every python.exe running as LOCAL SERVICE. Only the API runs Python
        under that account; your own Python (apply_schema.py, a test run) runs
        as your admin account and is never touched;
      - whatever process is listening on port 8000.
    Filtering by command line (e.g. *shoplite\api*) misses them: the venv
    launcher starts the base "C:\Program Files\Python312\python.exe", and
    multiprocessing workers start with a generic command line.

    A process on port 8000 that is NOT Python is never killed; the script
    stops and tells you what it is.

    Exit code 0 when the API is healthy (and on -ExpectedVersion, if given),
    1 otherwise.

.PARAMETER ExpectedVersion
    If given (deploy.ps1 passes it), /health must report this version.

.PARAMETER HealthTimeoutSeconds
    How long to wait for a healthy API after starting. Default 60.

.EXAMPLE
    .\restart-api.ps1
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$ExpectedVersion = '',
    [int]$HealthTimeoutSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$serviceName = 'shoplite-api'
$port        = 8000
$healthUrl   = "http://127.0.0.1:$port/health"
$errorLog    = 'F:\Logs\shoplite-api\shoplite-api.err.log'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "[OK]   $Message" -ForegroundColor Green }
function Write-Fix([string]$Message)  { Write-Host "[FIX]  $Message" -ForegroundColor Yellow }
function Stop-Restart([string]$Message) {
    Write-Host "`n[FAIL] $Message" -ForegroundColor Red
    exit 1
}

# PIDs of processes listening on the API port (empty array if none).
function Get-PortOwnerIds {
    @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

# python.exe processes running as LOCAL SERVICE: the API and its children.
function Get-ApiPythonProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
        $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction SilentlyContinue
        $owner -and $owner.User -eq 'LOCAL SERVICE'
    })
}

function Show-Process($Process) {
    $started = if ($Process.CreationDate) { $Process.CreationDate.ToString('yyyy-MM-dd HH:mm:ss') } else { '?' }
    Write-Host ("       PID {0,-6} started {1} UTC  {2}" -f $Process.ProcessId, $started, $Process.CommandLine)
}

if (-not (Get-Service -Name $serviceName -ErrorAction SilentlyContinue)) {
    Stop-Restart "The $serviceName service isn't installed (see README.md, First-time install)."
}

# -----------------------------------------------------------------------------
# 1. Stop the service
# -----------------------------------------------------------------------------
Write-Step "Stopping $serviceName"
$service = Get-Service -Name $serviceName
if ($service.Status -ne 'Stopped') {
    Stop-Service -Name $serviceName
    $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
}
Write-Ok "Service stopped."

# -----------------------------------------------------------------------------
# 2. Leftovers: kill old API processes, then confirm port 8000 is free
# -----------------------------------------------------------------------------
Write-Step "Checking for leftover API processes and port $port"

$leftovers = @(Get-ApiPythonProcesses)
$portOwnerIds = @(Get-PortOwnerIds)

# Anything on the port that isn't one of the API's Python processes?
foreach ($ownerId in $portOwnerIds) {
    if (@($leftovers | Where-Object { $_.ProcessId -eq $ownerId }).Count -eq 0) {
        $other = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId" -ErrorAction SilentlyContinue
        if ($other -and $other.Name -eq 'python.exe') {
            $leftovers += $other          # Python under another account: still an old API copy
        }
        elseif ($other) {
            Stop-Restart ("Port $port is used by $($other.Name) (PID $ownerId), which isn't the API. " +
                          "Not killing it. Find out what it is: Get-Process -Id $ownerId | Format-List *")
        }
    }
}

if ($leftovers.Count -eq 0) {
    Write-Ok "No API processes left; port $port is free."
}
else {
    Write-Fix "$($leftovers.Count) API process(es) survived the stop. Killing them:"
    foreach ($process in $leftovers) {
        Show-Process $process
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    # Wait until they're gone and the port is released.
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and
           ((@(Get-ApiPythonProcesses).Count -gt 0) -or (@(Get-PortOwnerIds).Count -gt 0))) {
        Start-Sleep -Milliseconds 500
    }
    if (@(Get-PortOwnerIds).Count -gt 0) {
        Stop-Restart "Port $port is still in use after killing the leftovers. Check: Get-NetTCPConnection -LocalPort $port"
    }
    Write-Ok "Leftovers killed; port $port is free."
}

# -----------------------------------------------------------------------------
# 3. Start the service
# -----------------------------------------------------------------------------
Write-Step "Starting $serviceName"
$startedAt = Get-Date
Start-Service -Name $serviceName
(Get-Service -Name $serviceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
Write-Ok "Service running."

# -----------------------------------------------------------------------------
# 4. Wait for /health
# -----------------------------------------------------------------------------
Write-Step "Health"
$deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
$last = 'no response yet'
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
        $body = $response.Content | ConvertFrom-Json
        $versionOk = (-not $ExpectedVersion) -or ($body.version -eq $ExpectedVersion)
        if ($body.status -eq 'ok' -and $versionOk) {
            Write-Ok "Healthy: status ok, database $($body.database), version $($body.version)."
            # Every API process should be new (started after this restart).
            $old = @(Get-ApiPythonProcesses | Where-Object { $_.CreationDate -lt $startedAt })
            if ($old.Count -gt 0) {
                Write-Warning "Some API processes are older than this restart; run this script again:"
                foreach ($process in $old) { Show-Process $process }
            }
            exit 0
        }
        $last = "status $($body.status), version $($body.version)"
    }
    catch {
        # 503 (database unreachable) or connection refused (still starting).
        $last = $_.Exception.Message
    }
}

Write-Host "`nLast health result: $last" -ForegroundColor Yellow
if (Test-Path -LiteralPath $errorLog) {
    Write-Host "`nLast 30 lines of ${errorLog}:" -ForegroundColor Yellow
    Get-Content -LiteralPath $errorLog -Tail 30
}
$what = if ($ExpectedVersion) { "healthy version $ExpectedVersion" } else { 'healthy' }
Stop-Restart "The API didn't report $what within $HealthTimeoutSeconds seconds."
