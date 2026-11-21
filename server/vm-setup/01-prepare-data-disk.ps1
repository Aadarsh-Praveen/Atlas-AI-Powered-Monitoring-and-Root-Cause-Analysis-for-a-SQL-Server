<#
.SYNOPSIS
    Formats prod-sql-01's data disk and creates the folders for SQL Server
    data, SQL Server logs, and application logs.

.DESCRIPTION
    Bicep attaches the data disk raw (infra/modules/vm.bicep). This script:
      1. Finds that raw disk (the only RAW disk on the VM) and checks its size.
      2. Initializes it (GPT), creates one partition, and formats it NTFS with
         a 64 KB allocation unit size, Microsoft's recommendation for SQL Server
         data and log files (SQL reads and writes in 64 KB extents).
      3. Creates:
           F:\SQLData   SQL Server data files (.mdf/.ndf)
           F:\SQLLog    SQL Server transaction logs (.ldf)
           F:\Logs      application logs (ShopLite API, Caddy), added in B3
      4. Gives the SQL Server service account (NT SERVICE\MSSQLSERVER) full
         control of the two SQL folders, so it can create database files there.

    Safe to re-run: if F: already exists with the SQLDATA label, formatting is
    skipped, and folders and permissions are only added if missing. The script
    never formats a disk that already has partitions.

    Drive letter: C: is Windows. D: is kept free for Azure's temporary disk on
    sizes that have one (wiped on deallocation, never for data); production's
    B2as_v2 has no temporary disk, but leaving D: alone keeps the layout the same
    on any size. F: is used for the data disk.

.PARAMETER DriveLetter
    Letter for the data volume. Default F.

.PARAMETER VolumeLabel
    Volume label, used to recognize the disk on re-runs. Default SQLDATA.

.PARAMETER ExpectedSizeGB
    Size of the data disk in Bicep (dataDiskSizeGB). Default 64. The script
    refuses to format a raw disk of a different size, in case it's the wrong one.

.EXAMPLE
    .\01-prepare-data-disk.ps1
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [char]$DriveLetter = 'F',
    [string]$VolumeLabel = 'SQLDATA',
    [int]$ExpectedSizeGB = 64
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "[OK]   $Message" -ForegroundColor Green }
function Write-Skip([string]$Message) { Write-Host "[SKIP] $Message" -ForegroundColor DarkGray }

# -----------------------------------------------------------------------------
# 1. Format the data disk (only if it isn't already)
# -----------------------------------------------------------------------------
Write-Step "Data disk -> ${DriveLetter}:"

$volume = Get-Volume -DriveLetter $DriveLetter -ErrorAction SilentlyContinue

if ($volume) {
    if ($volume.FileSystemLabel -eq $VolumeLabel) {
        Write-Skip "${DriveLetter}: already exists with label $VolumeLabel; not formatting."
    }
    else {
        throw ("${DriveLetter}: already exists with label '$($volume.FileSystemLabel)', so it isn't the data disk. " +
               "Re-run with another letter, e.g. .\01-prepare-data-disk.ps1 -DriveLetter G")
    }
}
else {
    # A brand-new Azure data disk has no partition table: PartitionStyle RAW.
    # The OS disk and Azure's temporary disk are already partitioned, so the
    # data disk should be the only RAW one.
    $rawDisks = @(Get-Disk | Where-Object { $_.PartitionStyle -eq 'RAW' })

    if ($rawDisks.Count -eq 0) {
        throw ("No raw disk found and ${DriveLetter}: doesn't exist. Is the data disk attached? " +
               "Run Get-Disk and check the VM's Disks page in the portal.")
    }
    if ($rawDisks.Count -gt 1) {
        throw "More than one raw disk found ($($rawDisks.Count)). Expected exactly one data disk; check Get-Disk."
    }

    $disk = $rawDisks[0]
    $sizeGB = [math]::Round($disk.Size / 1GB)
    if ([math]::Abs($sizeGB - $ExpectedSizeGB) -gt 2) {
        throw "The raw disk is $sizeGB GB but $ExpectedSizeGB GB was expected. Not formatting it; pass -ExpectedSizeGB if you resized it."
    }

    Write-Host "Found raw disk $($disk.Number) ($sizeGB GB). Initializing and formatting..."
    Initialize-Disk -Number $disk.Number -PartitionStyle GPT
    New-Partition -DiskNumber $disk.Number -UseMaximumSize -DriveLetter $DriveLetter | Out-Null
    # 64 KB clusters: matches SQL Server's 64 KB extents (8 pages of 8 KB).
    Format-Volume -DriveLetter $DriveLetter -FileSystem NTFS -AllocationUnitSize 65536 `
                  -NewFileSystemLabel $VolumeLabel -Confirm:$false | Out-Null
    Write-Ok "Disk $($disk.Number) formatted as ${DriveLetter}: (NTFS, 64 KB clusters, label $VolumeLabel)."
}

# -----------------------------------------------------------------------------
# 2. Folders
# -----------------------------------------------------------------------------
Write-Step "Folders"

$sqlDataPath = "${DriveLetter}:\SQLData"
$sqlLogPath  = "${DriveLetter}:\SQLLog"
$appLogPath  = "${DriveLetter}:\Logs"

foreach ($path in @($sqlDataPath, $sqlLogPath, $appLogPath)) {
    if (Test-Path -LiteralPath $path) {
        Write-Skip "$path exists."
    }
    else {
        New-Item -ItemType Directory -Path $path | Out-Null
        Write-Ok "Created $path"
    }
}

# -----------------------------------------------------------------------------
# 3. Permissions for SQL Server on its folders
# -----------------------------------------------------------------------------
# SQL Server runs as the virtual account NT SERVICE\MSSQLSERVER, which can't
# write to new folders by default. (OI)(CI)F = full control, inherited by every
# file and subfolder created inside. The app log folder is handled in B3, when
# the API and Caddy services and their accounts exist.
Write-Step "Permissions for SQL Server"

$sqlServiceAccount = 'NT SERVICE\MSSQLSERVER'
if (-not (Get-Service -Name 'MSSQLSERVER' -ErrorAction SilentlyContinue)) {
    throw "The SQL Server service (MSSQLSERVER) wasn't found. This script expects the SQL Server 2022 marketplace image."
}

foreach ($path in @($sqlDataPath, $sqlLogPath)) {
    $acl = Get-Acl -LiteralPath $path
    $hasAccess = $acl.Access | Where-Object {
        $_.IdentityReference.Value -eq $sqlServiceAccount -and
        $_.FileSystemRights.ToString() -match 'FullControl' -and
        $_.AccessControlType -eq 'Allow'
    }
    if ($hasAccess) {
        Write-Skip "$sqlServiceAccount already has full control of $path."
    }
    else {
        & icacls.exe $path /grant "${sqlServiceAccount}:(OI)(CI)F" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls failed to grant $sqlServiceAccount on $path." }
        Write-Ok "Granted $sqlServiceAccount full control of $path."
    }
}

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Step "Done"
$volume = Get-Volume -DriveLetter $DriveLetter
$freeGB  = [math]::Round($volume.SizeRemaining / 1GB, 1)
$totalGB = [math]::Round($volume.Size / 1GB, 1)
Write-Host "${DriveLetter}: $($volume.FileSystemLabel)  $freeGB GB free of $totalGB GB"
Write-Host "SQL data: $sqlDataPath   SQL logs: $sqlLogPath   App logs: $appLogPath"
Write-Host "Next: .\02-configure-sql.ps1"
