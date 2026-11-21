// infra/modules/sql-vm.bicep: registers prod-sql-01 with the SQL IaaS Agent
// extension.
//
// This creates a "SQL virtual machine" resource next to the VM. Through it,
// Azure configures SQL Server for us and shows SQL details in the portal.
// We use it for two settings:
//   - SQL authentication ON, with a SQL admin login we name (sysadmin). The
//     database tools (apply_schema.py, seed.py) log in with it, via
//     SQL_ADMIN_USER and SA_PASSWORD in the root .env on the VM. The API
//     never uses it: the API logs in as the least-privilege shoplite_app.
//   - Connectivity LOCAL: SQL Server accepts connections only from the VM
//     itself. The API runs on the same VM, so nothing outside ever needs
//     the database. Together with the NSG's Deny-SQL-Inbound rule and
//     Windows Firewall, port 1433 is closed from three directions.
//
// Storage settings are deliberately NOT managed here: the data disk is
// formatted by server/vm-setup/01-prepare-data-disk.ps1, and SQL Server's
// default paths and max memory are set by 02-configure-sql.ps1.

@description('Azure region (must match the VM).')
param location string

@description('Tags applied to the SQL virtual machine resource.')
param tags object

@description('Name of the VM to register. The SQL virtual machine resource uses the same name.')
param vmName string

@description('Resource ID of the VM to register.')
param vmId string

@description('Name of the SQL admin login (sysadmin). Azure rejects "sa" and other reserved names; choose your own, e.g. shoplite_admin.')
param sqlAdminUsername string

@description('Password for the SQL admin login. Passed at deploy time, never stored in a file.')
@secure()
param sqlAdminPassword string

resource sqlVm 'Microsoft.SqlVirtualMachine/sqlVirtualMachines@2022-02-01' = {
  name: vmName
  location: location
  tags: tags
  properties: {
    virtualMachineResourceId: vmId
    // Developer edition is free; PAYG here means "license comes with the
    // image" (no Software Assurance license of our own to apply).
    sqlServerLicenseType: 'PAYG'
    sqlImageSku: 'Developer'
    sqlManagement: 'Full'
    serverConfigurationsManagementSettings: {
      sqlConnectivityUpdateSettings: {
        connectivityType: 'LOCAL'
        port: 1433
        sqlAuthUpdateUserName: sqlAdminUsername
        sqlAuthUpdatePassword: sqlAdminPassword
      }
    }
  }
}

@description('Resource ID of the SQL virtual machine resource.')
output sqlVmId string = sqlVm.id
