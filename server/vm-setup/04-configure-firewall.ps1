<#
.SYNOPSIS
    Windows Firewall rules on prod-sql-01: allow 80 and 443 in, block 1433
    from outside the VM.

.DESCRIPTION
    The second layer behind the Azure NSG (infra/modules/network.bicep). Both
    layers say the same thing, so a mistake in one doesn't expose the server:

      Port  NSG (Azure)               Windows Firewall (this script)
      443   allow from internet       allow   (Caddy, HTTPS for the API)
      80    allow from internet       allow   (Let's Encrypt challenges; Caddy
                                               redirects everything else to 443)
      3389  allow from your IP only   left to Windows' built-in Remote Desktop
                                       rules, which the image already enables
      1433  deny (first rule)         BLOCK for remote addresses

    The 1433 rule blocks connections from any remote address. Connections from
    the VM itself (the API, sqlcmd, SSMS on the VM) use the loopback interface,
    which Windows Firewall doesn't filter, so they keep working. In Windows
    Firewall a block rule always wins over an allow rule, so no other rule can
    re-open 1433.

    Also turns the firewall on for all three profiles (Domain, Private,
    Public) in case it was off.

    Safe to re-run: rules are matched by their display names and updated in
    place; nothing is duplicated.

.EXAMPLE
    .\04-configure-firewall.ps1
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "[OK]   $Message" -ForegroundColor Green }
function Write-Skip([string]$Message) { Write-Host "[SKIP] $Message" -ForegroundColor DarkGray }

# All rules this script owns share this group, so they're easy to find in
# "Windows Defender Firewall with Advanced Security" (filter by group).
$group = 'ShopLite'

# -----------------------------------------------------------------------------
# 1. Firewall on for every profile
# -----------------------------------------------------------------------------
Write-Step "Firewall profiles"
$offProfiles = @(Get-NetFirewallProfile | Where-Object { -not $_.Enabled })
if ($offProfiles.Count -eq 0) {
    Write-Skip "Domain, Private, and Public profiles are already on."
}
else {
    Set-NetFirewallProfile -Profile Domain, Private, Public -Enabled True
    Write-Ok "Turned on: $(($offProfiles | ForEach-Object Name) -join ', ')."
}

# -----------------------------------------------------------------------------
# 2. The rules
# -----------------------------------------------------------------------------
# Create the rule, or bring an existing rule with the same name back to these
# exact settings.
function Set-ShopLiteRule {
    param(
        [string]$Name,
        [int]$Port,
        [ValidateSet('Allow', 'Block')] [string]$Action,
        [string]$RemoteAddress,
        [string]$Description
    )
    $existing = Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Set-NetFirewallRule -DisplayName $Name -Direction Inbound -Action $Action -Enabled True `
                            -Profile Any -Description $Description
        $existing | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
        $existing | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress $RemoteAddress
        Write-Skip "$Name exists; settings re-applied."
    }
    else {
        New-NetFirewallRule -DisplayName $Name -Group $group -Direction Inbound -Action $Action `
                            -Protocol TCP -LocalPort $Port -RemoteAddress $RemoteAddress `
                            -Profile Any -Enabled True -Description $Description | Out-Null
        Write-Ok "Created $Name."
    }
}

Write-Step "Rules"
Set-ShopLiteRule -Name 'ShopLite - HTTPS in (443)' -Port 443 -Action Allow -RemoteAddress 'Any' `
    -Description 'Caddy: HTTPS for the ShopLite API. Mirrors the NSG rule Allow-HTTPS-Inbound.'
Set-ShopLiteRule -Name 'ShopLite - HTTP in (80)' -Port 80 -Action Allow -RemoteAddress 'Any' `
    -Description "Caddy: Let's Encrypt challenges and redirect to HTTPS. Mirrors Allow-HTTP-Inbound."
# Block rules win over allow rules in Windows Firewall, so this keeps 1433
# closed to remote machines whatever else is configured. Local connections
# (loopback) aren't filtered.
Set-ShopLiteRule -Name 'ShopLite - Block SQL from outside (1433)' -Port 1433 -Action Block -RemoteAddress 'Any' `
    -Description 'SQL Server is local-only; the API connects on this VM. Mirrors the NSG rule Deny-SQL-Inbound.'

# -----------------------------------------------------------------------------
# 3. Report any OTHER rule that would allow 1433 (the block still wins, but an
#    allow rule for 1433 is a sign someone tried to open it)
# -----------------------------------------------------------------------------
Write-Step "Other rules touching 1433"
$others = @(
    Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
        Where-Object { $_.DisplayGroup -ne $group } |
        Where-Object {
            $ports = @(($_ | Get-NetFirewallPortFilter).LocalPort)
            $ports -contains '1433'
        }
)
if ($others.Count -eq 0) {
    Write-Ok "No other enabled rule allows 1433."
}
else {
    foreach ($rule in $others) {
        Write-Warning "Rule '$($rule.DisplayName)' allows 1433. The ShopLite block rule overrides it, but consider disabling it: Disable-NetFirewallRule -DisplayName '$($rule.DisplayName)'"
    }
}

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Step "Done"
Get-NetFirewallRule -Group $group |
    Select-Object DisplayName, Enabled, Action |
    Format-Table -AutoSize
Write-Host "Remote Desktop (3389) uses Windows' built-in rules; the NSG limits it to your IP."
Write-Host "Setup scripts finished. Continue with server/vm-setup/README.md, step 5 (clone the repo)."
