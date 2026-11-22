// infra/modules/storage.bicep: where SQL Server's backups go (Phase 4).
//
// prod-sql-01 writes its backups with BACKUP ... TO URL straight into a
// private blob container here, using a SQL Server credential that holds a
// SAS token limited to this container (server/backups/). Off the VM, a
// backup survives the VM, its disks, or the whole SQL Server being lost.
//
// What it creates:
//   - A Standard LRS StorageV2 account: three copies inside one datacenter,
//     the cheapest redundancy. Fine for a learning project; a real company
//     would pick ZRS or GRS to survive a datacenter or region outage.
//   - A container (default "sqlbackups") with NO public access.
//   - A lifecycle rule that deletes backups older than retentionDays, so
//     storage stays small without anyone cleaning up.
//
// Access is locked down at the account level too: no anonymous blob access,
// HTTPS only, TLS 1.2 minimum. Shared-key access stays ON because the SAS
// for SQL Server is signed with the account key: Azure's alternative (a
// "user delegation" SAS) lasts at most 7 days, too short for a credential
// that SQL Server uses for months. Nothing else uses the keys.

@description('Azure region (same as the VM: backups don\'t cross regions, and same-region traffic is free).')
param location string

@description('Tags applied to every resource.')
param tags object

@description('Storage account name: 3-24 lowercase letters and digits, unique across ALL of Azure.')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Private container for the backup files.')
param containerName string = 'sqlbackups'

@description('Backups older than this many days are deleted automatically.')
@minValue(1)
param retentionDays int = 14

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    // Hot, not Cool: backups are written every 15 minutes and kept only 14
    // days. Cool charges more per write and bills a 30-day minimum for
    // every blob, so it would cost MORE here.
    accessTier: 'Hot'
    allowBlobPublicAccess: false          // no container can ever be made public
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowSharedKeyAccess: true            // needed to sign the service SAS (see header)
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

// Lifecycle management: Azure checks the rule about once a day, so a file
// can live a day or so past retentionDays. That's the expected behavior.
// SQL Server writes block blobs when it uses a SAS credential, which is
// what the rule matches.
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'delete-old-backups'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [ 'blockBlob' ]
              prefixMatch: [ '${containerName}/' ]
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: retentionDays
                }
              }
            }
          }
        }
      ]
    }
  }
  dependsOn: [ container ]
}

@description('Name of the storage account.')
output storageAccountName string = storage.name

@description('URL of the backup container: what BACKUP ... TO URL and the SQL credential use.')
output containerUrl string = '${storage.properties.primaryEndpoints.blob}${containerName}'
