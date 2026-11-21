// infra/modules/network.bicep: the network around prod-sql-01.
//
// Creates:
//   - a network security group (NSG): the cloud firewall for the subnet
//   - a virtual network with one subnet, protected by that NSG
//   - a static public IP with a DNS label, so the VM has a stable name:
//       <dnsLabel>.<region>.cloudapp.azure.com
//   (production: shoplite-aadarsh.northcentralus.cloudapp.azure.com)
//
// What the internet can reach (everything else is blocked):
//   443  HTTPS   from anywhere   -> Caddy -> the ShopLite API
//   80   HTTP    from anywhere   -> only so Let's Encrypt can issue and renew
//                                   the certificate; Caddy redirects the rest
//   3389 RDP     from YOUR IP    -> remote desktop for administration
//   1433 SQL     never           -> SQL Server is local-only (see sql-vm.bicep)
//
// Windows Firewall on the VM mirrors these rules (server/vm-setup/
// 04-configure-firewall.ps1): two layers of the same protection.
//
// Rule order matters: Azure evaluates NSG rules from the LOWEST priority
// number up and stops at the first match. The 1433 deny therefore has the
// lowest number (100), so no allow rule can ever open the database port,
// even one added later by mistake.

@description('Azure region for every resource in this module.')
param location string

@description('Tags applied to every resource (project, env, app).')
param tags object

@description('Base name used for the network resources, e.g. prod-sql-01.')
param vmName string

@description('DNS label for the public IP: <dnsLabel>.<region>.cloudapp.azure.com. Lowercase letters, digits, and hyphens; must be unique in the region.')
@minLength(3)
@maxLength(63)
param dnsLabel string

@description('Your own public IP address in CIDR form (e.g. 203.0.113.25/32). Only this address may use RDP.')
param rdpSourceAddressPrefix string

@description('Address space for the virtual network.')
param vnetAddressPrefix string = '10.10.0.0/16'

@description('Address range for the VM subnet (inside the virtual network).')
param subnetAddressPrefix string = '10.10.1.0/24'

var nsgName = 'nsg-${vmName}'
var vnetName = 'vnet-shoplite-prod'
var subnetName = 'snet-sql'
var publicIpName = 'pip-${vmName}'

// -----------------------------------------------------------------------------
// Network security group. Lower priority numbers are checked first and the
// first matching rule wins, so the deny for 1433 comes first. Azure's
// built-in default rules (priority 65000+) deny all other inbound traffic
// from the internet.
// -----------------------------------------------------------------------------
resource nsg 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: nsgName
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        // Checked first (lowest number). The default rules would already
        // block 1433, but an allow rule with a lower number than a deny would
        // win. With the deny at 100, no allow rule added later (for example a
        // broad "allow all from my IP") can open the database port. It also
        // documents the decision on the portal's NSG page.
        name: 'Deny-SQL-Inbound'
        properties: {
          description: 'SQL Server is never reachable from outside the VM; the API connects locally.'
          priority: 100
          direction: 'Inbound'
          access: 'Deny'
          protocol: 'Tcp'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '1433'
        }
      }
      {
        name: 'Allow-HTTPS-Inbound'
        properties: {
          description: 'HTTPS to Caddy, which forwards to the ShopLite API on 127.0.0.1:8000.'
          priority: 110
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'Allow-HTTP-Inbound'
        properties: {
          description: 'Let\'s Encrypt certificate challenges only; Caddy redirects all other HTTP to HTTPS.'
          priority: 120
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '80'
        }
      }
      {
        name: 'Allow-RDP-From-Owner'
        properties: {
          description: 'Remote desktop from the owner\'s public IP only. Update rdpSourceAddressPrefix and redeploy if your IP changes.'
          priority: 130
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: rdpSourceAddressPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '3389'
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// Virtual network with one subnet. The NSG is attached to the subnet, so it
// protects every machine placed there (just the VM, for now).
// -----------------------------------------------------------------------------
resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: subnetName
        properties: {
          addressPrefix: subnetAddressPrefix
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// Public IP. Standard SKU + Static: the address never changes while the
// resource exists (even when the VM is deallocated), and Standard IPs are
// closed by default, so only what the NSG allows gets through.
// -----------------------------------------------------------------------------
resource publicIp 'Microsoft.Network/publicIPAddresses@2023-09-01' = {
  name: publicIpName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    dnsSettings: {
      domainNameLabel: dnsLabel
    }
  }
}

@description('Resource ID of the VM subnet.')
output subnetId string = vnet.properties.subnets[0].id

@description('Resource ID of the public IP (attached to the VM\'s network card).')
output publicIpId string = publicIp.id

@description('The VM\'s public DNS name, used by Caddy, Vercel, and RDP.')
output fqdn string = publicIp.properties.dnsSettings.fqdn

@description('Name of the network security group.')
output nsgName string = nsg.name
