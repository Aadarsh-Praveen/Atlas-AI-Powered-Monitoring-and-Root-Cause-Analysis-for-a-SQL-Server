# The VM's business hours in Azure: two Logic Apps

Two small **Consumption Logic Apps** start `prod-sql-01` at **13:00 UTC** and deallocate it at
**23:00 UTC**, Monday to Friday. They replace the schedule in `.github/workflows/vm-schedule.yml`,
which stays for **manual** runs (maintenance), with its time guard.

**Why move it out of GitHub:** GitHub's scheduler can run hours late or not at all. On Monday
2026-11-23 the 13:00 start arrived at 00:30 UTC the next day (and started the VM at night), the 23:00
deallocate never ran, and only the Azure auto-shutdown at 04:00 stopped the VM. A Logic App's
Recurrence trigger runs inside Azure, next to the VM.

> **You create everything in this guide by hand in the Azure portal** (https://portal.azure.com), as an
> account that is **Owner** (or User Access Administrator) on the subscription: the role assignments
> need that. No Bicep: you see every piece. Claude Code never changes Azure.

| What | Name | Notes |
|---|---|---|
| Logic App (Consumption) | `logic-shoplite-vm-start` | Recurrence Mon–Fri 13:00 UTC → HTTP `POST …/start` |
| Logic App (Consumption) | `logic-shoplite-vm-deallocate` | Recurrence Mon–Fri 23:00 UTC → HTTP `POST …/deallocate` |
| Identity | each app's **system-assigned managed identity** | No secret anywhere; deleted with its app |
| Role assignment (×2) | **ShopLite VM Operator** on `prod-sql-01` only | The custom role from `infra/github-oidc.md` step 4: read, instance view, start, deallocate. Nothing else. |

Both go in **`rg-shoplite-prod`**, region **North Central US**, tags **`project=atlas`, `env=prod`,
`app=shoplite`**. The **04:00 UTC auto-shutdown stays** as the safety net.

```
Recurrence (Azure, Mon–Fri 13:00 / 23:00 UTC)
   ▼
HTTP action, signed in as the app's own managed identity (audience: Azure Resource Manager)
   ▼
POST https://management.azure.com/.../virtualMachines/prod-sql-01/start   (or /deallocate)
   │  Azure answers 202 Accepted + Location; the action keeps checking until the VM is
   ▼  really running (or deallocated), so a green run means it's done
Role "ShopLite VM Operator" on prod-sql-01: the only thing the identity may touch
```

Facts below are from Microsoft's documentation, read 2026-11-24 ("Schedule and run recurring workflows
with the Recurrence trigger", "Authenticate access with managed identities", "Virtual Machines - Start /
Deallocate", "Usage metering, billing, and pricing for Azure Logic Apps", "View workflow status and run
history").

---

## Before you start

- The custom role **ShopLite VM Operator** exists (it does, from `infra/github-oidc.md` step 4).
- You know the **subscription ID** (portal → Subscriptions).
- **Read this first: saving can run the workflow immediately.** Microsoft: without a start time, "the
  first recurrence immediately runs when you save the workflow." Step 4 sets a start time **before the
  first save**. As a second line of defence, do this guide **when the VM is off** (an accidental
  deallocate is then harmless; an accidental start you simply undo).

## 1. Create `logic-shoplite-vm-start`

**Create a resource** → **Logic App** → **Create**:

| Field | Value |
|---|---|
| Plan | **Consumption** (Multi-tenant) |
| Resource group | `rg-shoplite-prod` |
| Logic App name | `logic-shoplite-vm-start` |
| Region | North Central US |
| Enable log analytics | **No** (the run history below is enough; diagnostics would add ingestion) |
| Tags | `project=atlas`, `env=prod`, `app=shoplite` |

**Review + create** → **Create**. Don't open the designer yet.

## 2. Turn on its managed identity

The app → **Settings** → **Identity** → **System assigned** → **On** → **Save** → **Yes**.
(Consumption apps don't turn it on by themselves.)

## 3. Give it the role, on the VM only

`prod-sql-01` (the **VM**, not the resource group) → **Access control (IAM)** → **Add** → **Add role
assignment**:
1. **Role** tab: search **ShopLite VM Operator** (custom roles appear in the list) → **Next**.
2. **Members** tab: **Assign access to** → **Managed identity** → **+ Select members** → subscription →
   **Managed identity**: **Logic app** → select `logic-shoplite-vm-start` → **Select**.
3. **Review + assign**.

It can take a few minutes to take effect.

## 4. The trigger: Recurrence, weekdays 13:00 UTC

The app → **Development Tools** → **Logic app designer** → **Add a trigger** → search **Recurrence**
(Schedule, built-in). Set **all** of these **before saving**:

| Field | Value |
|---|---|
| Interval / Frequency | **1** / **Week** |
| On these days | Monday, Tuesday, Wednesday, Thursday, Friday |
| At these hours | **13** |
| At these minutes | **0** (set it: otherwise the minute comes from the start time) |
| Start time | the **next weekday** in this exact form, ending in **Z**: e.g. `2026-11-25T13:00:00Z` |
| Time zone | leave empty |

**Why the `Z`:** the trigger "doesn't accept a UTC offset", and with a `Z` it "ignores the time zone
value", so the schedule is in UTC like everything else in ShopLite. Microsoft's page is ambiguous about
daylight saving without a time zone, so **after the next change (March 2027) check the trigger history**
(step 8) still shows 13:00 and 23:00 UTC. Microsoft notes a run may start "up to 1 minute" from the
scheduled time.

## 5. The action: HTTP POST to start the VM

Under the trigger: **+** → **Add an action** → search **HTTP** (built-in) → **HTTP**:

| Field | Value |
|---|---|
| Method | **POST** |
| URI | `https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/rg-shoplite-prod/providers/Microsoft.Compute/virtualMachines/prod-sql-01/start?api-version=2026-04-01` |
| Headers, Queries, Body | none |
| Advanced parameters → **Authentication** | Authentication type **Managed identity**; Managed identity **System-assigned managed identity**; Audience **`https://management.azure.com/`** (the default; it must match **exactly**, trailing slash included, or Azure answers 400/401) |

Leave the action's **Settings** as they are: **Asynchronous pattern On** (the default) makes the action
follow Azure's `202 Accepted` and check until the VM is running, so the run turns green only when it's
done. The default retry policy retries a few times on transient errors.

**Save** (top left). Rename the action to `Start prod-sql-01` (its **…** menu) and save again: the name
appears in the run history.

## 6. The second app: `logic-shoplite-vm-deallocate`

Repeat steps 1–5 with:
- name **`logic-shoplite-vm-deallocate`**;
- its own identity (step 2) and **its own** role assignment on `prod-sql-01` (step 3: select this app);
- **At these hours `23`**, start time e.g. `2026-11-25T23:00:00Z`;
- URI ending in **`/deallocate?api-version=2026-04-01`** (not `/powerOff`: deallocate releases the
  compute, so it stops billing; `forceDeallocate` stays at its default, false), action name
  `Deallocate prod-sql-01`.

**Late runs and the deallocate app:** GitHub's `vm-schedule` skips a *scheduled* deallocate that
arrives during business hours (it would be yesterday's, hours late). The Logic App needs no such guard:
its trigger fires within about a minute of 23:00, and an Azure-side delay long enough to reach the next
business day isn't a realistic case (the 04:00 auto-shutdown would have stopped the VM long before). But
**Run** (step 7) and **Resubmit** (step 8) deallocate **immediately**, whatever the time, like a manual
deallocate in GitHub: use them deliberately, outside business hours or when stopping is fine.

## 7. Test each one by hand

You don't have to wait for the schedule: the app → **Overview** → **Run** → **Run**.

1. **Deallocate app**, when stopping is harmless (outside business hours, or traffic paused): **Run**.
   Within a few minutes the run is **Succeeded** and `prod-sql-01` → **Overview** shows **Stopped
   (deallocated)**.
2. **Start app**, inside business hours (the VM off): **Run**. The run succeeds once the VM is
   **Running**; a few minutes later `https://shoplite-aadarsh.northcentralus.cloudapp.azure.com/health`
   answers `"status":"ok"` (the services start on their own after boot).
3. Check each identity has **exactly one** role assignment: the app → **Identity** → **Azure role
   assignments** → one row, **ShopLite VM Operator**, scope `prod-sql-01`.

Then let one **real scheduled day** run (13:00 and 23:00) and confirm both in the trigger history
before step 9.

## 8. Reading the history

The app → **Overview**:
- **Trigger history**: every scheduled firing (**Succeeded** / **Fired**, **Skipped**, **Failed**) with
  its time. This is where you check 13:00 / 23:00 UTC.
- **Runs history**: every run. Select one to see each step; select the HTTP action for its **Inputs**
  (the URI) and **Outputs** (status code and body). **Succeeded with retries** means a transient error
  was retried. **Resubmit** reruns it (immediately: see step 6).

| HTTP action output | Cause and fix |
|---|---|
| `403`, `AuthorizationFailed ... virtualMachines/start/action` | The role isn't assigned to **this** app's identity on the VM, or not yet in effect (wait 5–10 minutes). Step 3, step 7.3 |
| `401` or `400` about the audience/token | Authentication not set to the system-assigned identity, or the audience isn't exactly `https://management.azure.com/` |
| `404` | A typo in the URI: subscription ID, resource group, VM name |
| `409` conflict | Another operation on the VM was running (e.g. the GitHub workflow at the same moment); the next run is fine. Avoid running both |
| Run **Failed** at the trigger | The app was saved without a valid start time or schedule: re-check step 4 |

**Failures are already alerted:** a failed deallocate leaves the VM running at night → the severity-1
alert `always-vm-running-off-hours` (always sent). A failed start → the API-health and collector
heartbeat alerts in business hours. No extra alert needed.

## 9. Then: GitHub's `vm-schedule` becomes manual only

After step 7 and one good scheduled day, tell Claude Code: the `schedule:` lines are removed from
`vm-schedule.yml` (and the "temporarily" notes from the docs). Until then both run; the time guard keeps
GitHub's runs harmless, and a start on an already running VM changes nothing. (If GitHub's 23:00 arrives
while the Logic App is still deallocating, one of them may get a `409`: harmless.)

## Keeping the VM off (maintenance, holidays)

The app `logic-shoplite-vm-start` → **Overview** → **Disable**. (Disabled apps aren't billed.)
**Enable** to resume. While GitHub's schedule still exists, disable the `vm-schedule` workflow too.
For a one-off start outside hours, use GitHub's `vm-schedule` with `force_start`.

## Cost

- Consumption apps bill **per execution**: here each run is **one trigger + one HTTP action**, both
  **built-in** operations (a retry counts as another execution). About 22 weekdays × 2 apps × 2 = **~90
  executions a month**.
- The pricing page lists **"First 4,000 actions free"**; Microsoft's usage page says "an *initial
  number* of free built-in operations, per Azure subscription" (neither says "monthly" explicitly).
  Microsoft doesn't say whether the action's checks of the `202` count as executions; even if each did,
  a start or deallocate takes a few minutes, far under 4,000 a month.
- Billing is per execution, so between runs nothing is metered; disabled apps are explicitly not
  metered. Storage is billed only for retained run history (inputs and outputs), which here is tiny.
- **Expected: $0.** Check the price above the free grant on
  https://azure.microsoft.com/pricing/details/logic-apps/ in a browser (the prices load in the page),
  and the subscription's **Cost analysis** filtered to the two apps after the first month.

## Undo

Delete the two Logic Apps (their identities go with them), then on `prod-sql-01` → **Access control
(IAM)** → **Role assignments**, remove the two rows now shown as **Identity not found**. Don't delete the
role **ShopLite VM Operator** while GitHub's identity still uses it. Before deleting the start app,
make sure something starts the VM (GitHub's schedule back, or by hand).

## Checklist

- [ ] `logic-shoplite-vm-start`: identity on; ShopLite VM Operator on `prod-sql-01` only; Recurrence Mon–Fri 13:00 with a `Z` start time; HTTP POST `…/start?api-version=2026-04-01`, managed identity, audience `https://management.azure.com/`
- [ ] `logic-shoplite-vm-deallocate`: the same with 23:00 and `…/deallocate`
- [ ] Both tested with **Run**; VM state and `/health` as expected
- [ ] One scheduled day seen in both trigger histories at 13:00 and 23:00 UTC
- [ ] Told Claude Code to make `vm-schedule` manual only
- [ ] Runbook section 4 updated with the date the Logic Apps took over
- [ ] March 2027: trigger history still at 13:00 / 23:00 UTC after daylight saving
