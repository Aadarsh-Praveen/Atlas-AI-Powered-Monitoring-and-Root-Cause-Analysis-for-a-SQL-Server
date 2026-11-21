# infra

Bicep templates for ShopLite's production infrastructure in Azure: the `prod-sql-01` VM and the
network around it, all in one resource group, `rg-shoplite-prod`. Later phases add monitoring and the
Atlas resources here.

> **You run every command in this guide.** Claude Code writes the templates but never runs `az` or
> deploys anything. Set up the budget alerts (Phase 2, Part C1) **before** the first deployment.

## Files

| File | What it is |
|---|---|
| `main.bicep` | The entry point you deploy. Defines the tags and secure password parameters, and wires the modules together. |
| `modules/network.bicep` | Network security group, virtual network and subnet, static public IP with your DNS label. |
| `modules/vm.bicep` | The VM, its network card, OS disk, and data disk. |
| `modules/sql-vm.bicep` | SQL IaaS Agent registration: SQL authentication and local-only connectivity. |
| `modules/auto-shutdown.bicep` | The daily shutdown schedule. |
| `prod.parameters.example.json` | Placeholders for the non-secret values. Copy it to `prod.parameters.local.json` (Git ignores `*.local.json`). |

## What gets created

Everything lands in `rg-shoplite-prod`, in one region, tagged `project=atlas`, `env=prod`,
`app=shoplite`.

| Resource | Name | Notes |
|---|---|---|
| Network security group | `nsg-prod-sql-01` | Rules in order: **deny 1433** (100), allow 443 (110), allow 80 (120), allow 3389 from your IP only (130). Everything else inbound is denied by Azure's defaults. |
| Virtual network | `vnet-shoplite-prod` | `10.10.0.0/16`, one subnet `snet-sql` (`10.10.1.0/24`) protected by the NSG. |
| Public IP | `pip-prod-sql-01` | Standard, static, with DNS name `<dnsLabel>.<region>.cloudapp.azure.com`. |
| Network interface | `nic-prod-sql-01` | Connects the VM to the subnet and the public IP. |
| Virtual machine | `prod-sql-01` | Standard_B2ms (2 vCPU, 8 GB RAM); SQL Server 2022 Developer on Windows Server 2022; time zone UTC; boot diagnostics on. |
| OS disk | `osdisk-prod-sql-01` | Standard SSD, drive C:. |
| Data disk | `datadisk-prod-sql-01-01` | 64 GB Standard SSD, left **raw**. `server/vm-setup/01-prepare-data-disk.ps1` formats it for SQL data, SQL logs, and app logs. |
| SQL virtual machine | `prod-sql-01` | The SQL IaaS Agent registration: SQL authentication on, with your SQL admin login; connectivity **LOCAL** (SQL Server accepts connections from the VM only). |
| Shutdown schedule | `shutdown-computevm-prod-sql-01` | Deallocates the VM daily at `shutdownTime` (UTC). Shown on the VM's **Auto-shutdown** page in the portal. |

**Port 1433 is closed three ways:** the NSG denies it first, SQL Server listens locally only, and
Windows Firewall blocks it (Phase 2, B2).

## Before you deploy

1. **Tools.** Install the Azure CLI and Bicep, then sign in:

   ```bash
   brew install azure-cli
   az bicep install
   az login
   az account show          # confirm the subscription is the one you expect
   ```

2. **Budget alerts first** (Part C1): Cost Management → Budgets, with email alerts. Confirm the
   emails arrive before creating anything.

3. **Fill in your parameters:**

   ```bash
   cp infra/prod.parameters.example.json infra/prod.parameters.local.json
   ```

   | Parameter | What to put |
   |---|---|
   | `adminUsername` | Windows admin for RDP. Azure rejects common names such as `admin`, `administrator`, `root`, `user`, `test`. |
   | `sqlAdminUsername` | The SQL admin login (sysadmin), e.g. `shoplite_admin`. Not `sa`. You'll put this in `SQL_ADMIN_USER` in the VM's root `.env`. |
   | `dnsLabel` | Lowercase letters, digits, hyphens, starting with a letter, e.g. `shoplite-yourname`. Must be unique in the region; the deployment fails with a clear message if it's taken. |
   | `rdpSourceAddressPrefix` | Your public IP with `/32`. Find it with `curl -s https://api.ipify.org`. |
   | `shutdownTime` | `HHmm`, 24-hour, **UTC**. `0100` is 8 PM in Boston in winter, 9 PM in summer. |
   | `shutdownNotificationEmail` | Optional; an email warned 30 minutes before shutdown. Leave `""` for none. |

4. **Choose two new passwords** in your password manager: the Windows admin password and the SQL
   admin password. Both are typed at the deploy prompt, never saved in a file.
   - Windows: 12+ characters with uppercase, lowercase, a digit, and a symbol.
   - SQL: the same complexity rules. Use the repo's safe symbols (`! _ - . + % ^ *`) and avoid
     `$ # " ' ` ; { } @` and spaces, which break `.env` files and connection strings.

## Deploy

Run from the repo root.

1. **Check the templates compile** (offline, nothing is sent to Azure):

   ```bash
   az bicep build --file infra/main.bicep --stdout > /dev/null && echo "Bicep OK"
   ```

2. **Create the resource group** with the same tags. Use one region near Boston, such as `eastus2`:

   ```bash
   az group create --name rg-shoplite-prod --location eastus2 \
     --tags project=atlas env=prod app=shoplite
   ```

3. **Preview the changes** with `what-if`. It lists what would be created and creates nothing. It
   prompts for the two passwords too.

   ```bash
   az deployment group what-if \
     --resource-group rg-shoplite-prod \
     --template-file infra/main.bicep \
     --parameters @infra/prod.parameters.local.json
   ```

4. **Deploy.** Type the two passwords at the prompts. It usually takes 10 to 20 minutes; the SQL IaaS
   registration is the slowest step.

   ```bash
   az deployment group create \
     --resource-group rg-shoplite-prod \
     --template-file infra/main.bicep \
     --parameters @infra/prod.parameters.local.json
   ```

5. **Read the outputs** (the DNS name and health URL):

   ```bash
   az deployment group show --resource-group rg-shoplite-prod --name main \
     --query properties.outputs
   ```

The VM starts running as soon as the deployment finishes, and **billing starts with it**.

## After deploying: check in the portal

Open `rg-shoplite-prod` and look at:

- **The VM:** size B2ms, two disks, the public IP and DNS name. The time zone (UTC) shows once you're
  connected.
- **The NSG → Inbound security rules:** `Deny-SQL-Inbound` at 100 first, then 443, 80, and 3389 from
  your IP, then Azure's defaults.
- **The SQL virtual machine** resource: Security configuration shows SQL authentication enabled and
  connectivity **Local**.
- **The VM → Auto-shutdown:** enabled, at your time, in UTC.
- **Tags** on each resource.

Then continue with `server/vm-setup/README.md` (Part C3).

## Day-to-day

```bash
# Start the VM (manual for now; Phase 3 automates it)
az vm start -g rg-shoplite-prod -n prod-sql-01

# Stop AND deallocate it (stops compute billing). "az vm stop" alone keeps billing.
az vm deallocate -g rg-shoplite-prod -n prod-sql-01

# Check its state: should say "VM deallocated" when you're done for the day
az vm get-instance-view -g rg-shoplite-prod -n prod-sql-01 \
  --query "instanceView.statuses[?starts_with(code, 'PowerState/')].displayStatus" -o tsv
```

## Changing something later

Edit the parameters file (or a template) and **run the same `az deployment group create` again.** Bicep
deployments are incremental: Azure compares the templates with what exists and changes only the
differences. Examples:

- **Your home IP changed** (RDP stops connecting): update `rdpSourceAddressPrefix` and redeploy.
- **A new shutdown time:** update `shutdownTime` and redeploy.

Some properties can't be changed in place. The image and the disk names are set at creation, and a
disk can be enlarged but never shrunk. Run `what-if` first whenever you change a template.

## What keeps billing when the VM is deallocated

Deallocating the VM (`az vm deallocate`) stops **compute** charges only. These resources keep billing
for as long as they exist, running or not:

- **Managed disks:** the OS disk and the 64 GB data disk (Standard SSD) are billed by size, every
  hour, whether or not the VM is on.
- **The static public IP:** a Standard static IP is billed hourly while it exists, even when nothing
  is using it. Keeping it is what keeps your DNS name and address stable.

Together these are a few dollars a month. To stop **all** charges, delete the resource group (below),
which removes the disks and the IP too.

## Tear everything down

This deletes **every** resource in the group, including the database on the data disk. It can't be
undone.

```bash
az group delete --name rg-shoplite-prod
```

The CLI asks for confirmation, and deletion takes a few minutes. Afterwards, check Cost Management
over the next day or two to confirm that nothing in the group is still billing. To rebuild, run the
Deploy steps again: same templates, same result. That's the point of infrastructure as code.
