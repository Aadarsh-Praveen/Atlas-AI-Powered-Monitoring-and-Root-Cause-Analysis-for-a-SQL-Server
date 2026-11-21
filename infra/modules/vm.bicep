// infra/modules/vm.bicep: the prod-sql-01 virtual machine.
//
// Creates:
//   - a network card (NIC) in the VM subnet, with the public IP attached
//   - the VM: SQL Server 2022 Developer on Windows Server 2022, B2as_v2
//     (2 vCPU AMD, 8 GB RAM), time zone UTC, boot diagnostics on
//   - its disks: the OS disk (C:) and ONE separate, empty data disk for SQL
//     Server's data and log files plus the application logs
//
// The data disk is left raw on purpose: server/vm-setup/
// 01-prepare-data-disk.ps1 formats it and creates the folders, so the layout
// is visible in a script you can read and re-run.

@description('Azure region for the VM and its disks.')
param location string

@description('Tags applied to every resource (project, env, app).')
param tags object

@description('VM name, also used as the Windows computer name (15 characters max).')
@maxLength(15)
param vmName string

@description('VM size. Standard_B2as_v2 = 2 vCPU (AMD), 8 GB RAM, burstable, no temporary disk.')
param vmSize string = 'Standard_B2as_v2'

@description('Windows administrator username for RDP. Not "admin" or "administrator" (Azure rejects common names).')
param adminUsername string

@description('Windows administrator password. Passed at deploy time, never stored in a file.')
@secure()
param adminPassword string

@description('Resource ID of the subnet the NIC joins.')
param subnetId string

@description('Resource ID of the public IP attached to the NIC.')
param publicIpId string

@description('Size of the data disk in GB. 64 GB holds the 1,000,000-order database (a few GB), its log, and rotated app logs with room to grow; it can be enlarged later, never shrunk.')
param dataDiskSizeGB int = 64

var nicName = 'nic-${vmName}'
var osDiskName = 'osdisk-${vmName}'
var dataDiskName = 'datadisk-${vmName}-01'

// -----------------------------------------------------------------------------
// Network card. No NSG here: the subnet's NSG already protects it, and one
// place for rules is easier to reason about than two.
// -----------------------------------------------------------------------------
resource nic 'Microsoft.Network/networkInterfaces@2023-09-01' = {
  name: nicName
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: subnetId
          }
          publicIPAddress: {
            id: publicIpId
          }
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// The VM.
// -----------------------------------------------------------------------------
resource vm 'Microsoft.Compute/virtualMachines@2023-09-01' = {
  name: vmName
  location: location
  tags: tags
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      adminPassword: adminPassword
      windowsConfiguration: {
        // UTC, so Windows, SQL Server, the API, and Azure Monitor timestamps
        // line up without any conversion.
        timeZone: 'UTC'
        provisionVMAgent: true
        enableAutomaticUpdates: true
        patchSettings: {
          // Windows Update installs patches on its own schedule. On a VM,
          // patching is our responsibility (IaaS), not Azure's.
          patchMode: 'AutomaticByOS'
        }
      }
    }
    storageProfile: {
      // Microsoft's marketplace image with SQL Server 2022 Developer already
      // installed. Developer edition is free (no SQL license cost) and has
      // every Enterprise feature; it may not serve real customers, which is
      // fine for a learning project.
      imageReference: {
        publisher: 'MicrosoftSQLServer'
        offer: 'sql2022-ws2022'
        sku: 'sqldev-gen2'
        version: 'latest'
      }
      osDisk: {
        name: osDiskName
        createOption: 'FromImage'
        caching: 'ReadWrite'
        managedDisk: {
          storageAccountType: 'StandardSSD_LRS'
        }
        // Delete the disk with the VM, so a teardown leaves nothing billing.
        deleteOption: 'Delete'
      }
      dataDisks: [
        {
          name: dataDiskName
          lun: 0
          createOption: 'Empty'
          diskSizeGB: dataDiskSizeGB
          // 64 GB Standard SSD by default (dataDiskSizeGB).
          // No host caching: this one disk holds both SQL data and SQL log
          // files, and log writes must reach the disk immediately. (With
          // separate disks, data files would use ReadOnly caching.)
          caching: 'None'
          managedDisk: {
            storageAccountType: 'StandardSSD_LRS'
          }
          deleteOption: 'Delete'
        }
      ]
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
          properties: {
            deleteOption: 'Delete'
          }
        }
      ]
    }
    diagnosticsProfile: {
      // Screenshots and serial log of the boot, stored in an Azure-managed
      // storage account (no storage account of our own to create or pay for).
      // Useful when the VM won't start or RDP won't connect.
      bootDiagnostics: {
        enabled: true
      }
    }
  }
}

@description('Resource ID of the VM (used by the SQL IaaS extension and the shutdown schedule).')
output vmId string = vm.id

@description('Name of the VM.')
output vmName string = vm.name
