# `/health` failing (availability test)

| Setting | Value |
|---|---|
| Test | **URL ping test** (classic) in `appi-shoplite-prod`: **free**; retired on 30 September 2028, replace it with a standard test before then |
| Rule name | `shoplite-api-health` (the alert created with the test, renamed) |
| Severity | **2** (suppressed outside business hours: the API is off with the VM at night) |

**Meaning:** Azure checks `https://shoplite-aadarsh.northcentralus.cloudapp.azure.com/health` from five
places in the world every 5 minutes. When at least 3 of the 5 fail, the API is down for shoppers
(VM, Caddy, the API service, or SQL Server behind `/health`).

**Why from outside:** every other signal comes from the VM itself. This is the only one that sees what
a shopper sees, and it works even when the VM can't report anything.

**Why the classic ping test:** it's free. Standard tests are billed per run (five locations every 5
minutes is about 43,000 runs a month); the classic test still runs until September 2028.

## Create the test

`appi-shoplite-prod` → **Investigate** → **Availability** → **Add Classic test**:

| Field | Value |
|---|---|
| Test name | `api-health` |
| SKU / test type | URL ping test |
| URL | `https://shoplite-aadarsh.northcentralus.cloudapp.azure.com/health` |
| Parse dependent requests | Off |
| Enable retries for availability test failures | On (one retry before a location counts as failed) |
| Test frequency | 5 minutes |
| Test locations | **5**: e.g. West US 2, Central US, East US, North Europe, UK South |
| Test timeout | 30 seconds |
| HTTP response | Status code must equal 200 |
| Alerts | **Enabled**, alert location threshold **3** (Microsoft's rule: locations minus 2) |

**Create.**

## Fix the alert it created

Creating the test creates a metric alert rule for it. Make it match the others: **Monitor** →
**Alerts** → **Alert rules** → find the rule for `api-health` → **Edit**:
- **Actions:** select `ag-shoplite-email`.
- **Details:** severity **2**, name `shoplite-api-health`.

(Severity 2 matters: the suppression rule then silences the nightly failures while the VM is off.)

**First checks:** the test's results in Availability (all locations, or one?), then `/health` yourself
(runbook, "Is it healthy?"): 503 = SQL Server unreachable, 502 = API down, timeout = VM or Caddy.
