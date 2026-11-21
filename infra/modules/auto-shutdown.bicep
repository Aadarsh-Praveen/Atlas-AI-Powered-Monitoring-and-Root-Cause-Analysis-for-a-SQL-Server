// infra/modules/auto-shutdown.bicep: Azure's built-in daily shutdown for
// prod-sql-01, so a forgotten VM can't run (and bill) all night.
//
// The schedule DEALLOCATES the VM, which stops compute charges (disks and
// the static public IP still bill; see infra/README.md). Starting the VM is
// manual for now; Phase 3 adds automated start schedules for simulated
// traffic.
//
// The resource type comes from DevTest Labs, but no lab is created: this is
// the same object the portal's "Auto-shutdown" blade on the VM manages. Its
// name must be exactly "shutdown-computevm-<vmName>" for the portal to show it.

@description('Azure region (must match the VM).')
param location string

@description('Tags applied to the schedule.')
param tags object

@description('Name of the VM to shut down.')
param vmName string

@description('Resource ID of the VM to shut down.')
param vmId string

@description('Daily shutdown time as HHmm in 24-hour format, in shutdownTimeZone. Default 0100 UTC = 8 PM in Boston in winter (9 PM in summer).')
@minLength(4)
@maxLength(4)
param shutdownTime string = '0100'

@description('Windows time zone ID for shutdownTime. UTC matches the VM and every log.')
param shutdownTimeZone string = 'UTC'

@description('Email for a warning 30 minutes before shutdown (time to postpone it in the portal). Leave empty for no email.')
param notificationEmail string = ''

var sendNotifications = !empty(notificationEmail)

resource shutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = {
  name: 'shutdown-computevm-${vmName}'
  location: location
  tags: tags
  properties: {
    status: 'Enabled'
    taskType: 'ComputeVmShutdownTask'
    dailyRecurrence: {
      time: shutdownTime
    }
    timeZoneId: shutdownTimeZone
    targetResourceId: vmId
    notificationSettings: sendNotifications ? {
      status: 'Enabled'
      timeInMinutes: 30
      emailRecipient: notificationEmail
      notificationLocale: 'en'
    } : {
      status: 'Disabled'
    }
  }
}

@description('Resource ID of the shutdown schedule.')
output scheduleId string = shutdown.id
