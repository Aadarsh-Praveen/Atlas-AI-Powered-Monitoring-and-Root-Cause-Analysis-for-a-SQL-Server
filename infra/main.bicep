// infra/main.bicep: everything inside the rg-shoplite-prod resource group.
//
// Deploy (the owner runs this; see infra/README.md, which also covers deploying
// main.json through the portal's "Deploy a custom template"):
//   az deployment group create \
//     --resource-group rg-shoplite-prod \
//     --template-file infra/main.bicep \
//     --parameters @infra/prod.parameters.local.json
// The CLI then PROMPTS for adminPassword and sqlAdminPassword: they're
// @secure() and deliberately absent from every file.
//
// What it builds, through modules (infra/modules/):
//   network.bicep        NSG (1433 denied first; 443, 80, RDP-from-your-IP),
//                        virtual network + subnet, static public IP with DNS label
//   vm.bicep             prod-sql-01: SQL Server 2022 Developer on Windows Server
//                        2022, B2as_v2, UTC, OS disk + 64 GB data disk, boot diagnostics
//   sql-vm.bicep         SQL IaaS Agent: SQL authentication, LOCAL connectivity
//   auto-shutdown.bicep  daily deallocation at shutdownTime
//   storage.bicep        Phase 4: storage account + private "sqlbackups"
//                        container + lifecycle rule (delete after 14 days)
//
// Every resource gets the same tags, so cost and ownership are visible in
// Cost Management and the portal.

targetScope = 'resourceGroup'

// -----------------------------------------------------------------------------
// Parameters (non-secret values come from prod.parameters.local.json)
// -----------------------------------------------------------------------------
@description('Azure region for every resource. Defaults to the resource group\'s region (production: northcentralus).')
param location string = resourceGroup().location

@description('VM name and Windows computer name.')
@maxLength(15)
param vmName string = 'prod-sql-01'

@description('VM size. Standard_B2as_v2 = 2 vCPU (AMD), 8 GB RAM, burstable; B2ms wasn\'t available to this subscription in the East US regions.')
param vmSize string = 'Standard_B2as_v2'

@description('Data disk size in GB (SQL data, SQL log, app logs).')
param dataDiskSizeGB int = 64

@description('Windows administrator username for RDP.')
param adminUsername string

@description('Windows administrator password. Prompted at deploy time.')
@secure()
param adminPassword string

@description('SQL admin login name (sysadmin) created by the SQL IaaS extension. Not "sa".')
param sqlAdminUsername string

@description('SQL admin login password. Prompted at deploy time.')
@secure()
param sqlAdminPassword string

@description('DNS label: the VM becomes <dnsLabel>.<region>.cloudapp.azure.com (production: shoplite-aadarsh.northcentralus.cloudapp.azure.com).')
param dnsLabel string

@description('Your public IP in CIDR form (e.g. 203.0.113.25/32); the only source allowed to RDP.')
param rdpSourceAddressPrefix string

@description('Daily shutdown time, HHmm 24-hour, in UTC.')
param shutdownTime string = '0100'

@description('Optional email warned 30 minutes before shutdown. Empty = no email.')
param shutdownNotificationEmail string = ''

@description('Backup storage account name (globally unique, 3-24 lowercase letters and digits). The default is derived from the resource group\'s ID: unique, and the same on every redeploy.')
@minLength(3)
@maxLength(24)
param backupStorageAccountName string = take('stshoplite${uniqueString(resourceGroup().id)}', 24)

@description('Days to keep backup files before the lifecycle rule deletes them.')
@minValue(1)
param backupRetentionDays int = 14

// -----------------------------------------------------------------------------
// Tags on every resource
// -----------------------------------------------------------------------------
var tags = {
  project: 'atlas'
  env: 'prod'
  app: 'shoplite'
}

// -----------------------------------------------------------------------------
// Modules. Outputs flow from one to the next (subnet and IP into the VM, the
// VM's ID into the SQL registration and the schedule); Bicep works out the
// order from those references.
// -----------------------------------------------------------------------------
module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    location: location
    tags: tags
    vmName: vmName
    dnsLabel: dnsLabel
    rdpSourceAddressPrefix: rdpSourceAddressPrefix
  }
}

module vm 'modules/vm.bicep' = {
  name: 'vm'
  params: {
    location: location
    tags: tags
    vmName: vmName
    vmSize: vmSize
    adminUsername: adminUsername
    adminPassword: adminPassword
    subnetId: network.outputs.subnetId
    publicIpId: network.outputs.publicIpId
    dataDiskSizeGB: dataDiskSizeGB
  }
}

module sqlVm 'modules/sql-vm.bicep' = {
  name: 'sql-vm'
  params: {
    location: location
    tags: tags
    vmName: vm.outputs.vmName
    vmId: vm.outputs.vmId
    sqlAdminUsername: sqlAdminUsername
    sqlAdminPassword: sqlAdminPassword
  }
}

module autoShutdown 'modules/auto-shutdown.bicep' = {
  name: 'auto-shutdown'
  params: {
    location: location
    tags: tags
    vmName: vm.outputs.vmName
    vmId: vm.outputs.vmId
    shutdownTime: shutdownTime
    notificationEmail: shutdownNotificationEmail
  }
}

// Phase 4: backup storage. Independent of the VM (no outputs passed between
// them): the VM reaches it over HTTPS with a SAS, set up on the VM itself.
module backupStorage 'modules/storage.bicep' = {
  name: 'backup-storage'
  params: {
    location: location
    tags: tags
    storageAccountName: backupStorageAccountName
    retentionDays: backupRetentionDays
  }
}

// -----------------------------------------------------------------------------
// Outputs: printed when the deployment finishes.
// -----------------------------------------------------------------------------
@description('Public DNS name of the VM: use it for RDP, Caddy, and NEXT_PUBLIC_API_URL.')
output fqdn string = network.outputs.fqdn

@description('The API health URL once Caddy and the API are installed (Part C4).')
output healthUrl string = 'https://${network.outputs.fqdn}/health'

@description('Name of the VM.')
output vmName string = vm.outputs.vmName

@description('Backup storage account name (for creating the SAS).')
output backupStorageAccountName string = backupStorage.outputs.storageAccountName

@description('Backup container URL: the SQL Server credential is named exactly this.')
output backupContainerUrl string = backupStorage.outputs.containerUrl
