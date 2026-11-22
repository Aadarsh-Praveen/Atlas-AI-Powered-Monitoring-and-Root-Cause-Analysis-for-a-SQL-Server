# Azure Monitor setup (Phase 5, Part C3)

Step-by-step portal instructions for everything Azure Monitor needs, with the exact names and
settings. **You create each resource by hand in the Azure portal** (https://portal.azure.com):
Phase 5 has no Bicep, so you see every piece. Do it in the order below. Alerts come after this, in
`observability/alerts/`; ADX and Grafana are in `observability/adx-grafana-setup.md`.

Everything goes into the existing resource group **`rg-shoplite-prod`**, region **North Central US**
(`northcentralus`), with the tags **`project=atlas`, `env=prod`, `app=shoplite`**, like Phase 2.

**The VM must be running for steps 5–8** (the identity, the agent install, and the collector), so do
those inside business hours, or start it by hand with `vm-schedule.yml`. Steps 1–4 don't need it.

## What you'll build

```
prod-sql-01 ─┬─ collector (managed identity) ──▶ dce-shoplite-prod ──▶ dcr-shoplite-sql ─┐
             │                                                     └─▶ dcr-shoplite-ops ─┤
             └─ Azure Monitor Agent ─────────────────────────────────▶ dcr-shoplite-vm ──┤
ShopLite API (B4) ──▶ appi-shoplite-prod (Application Insights) ─────────────────────────┤
                                                                                          ▼
                                                              law-shoplite-prod (Log Analytics)
```

| Resource | Name | Why |
|---|---|---|
| Log Analytics workspace | `law-shoplite-prod` | Where all telemetry lands (31 days); the alert rules query it |
| Application Insights | `appi-shoplite-prod` | The API's requests, latency, errors, SQL calls (B4); stored in the workspace |
| Data collection endpoint (DCE) | `dce-shoplite-prod` | The URL the collector sends to. The portal's custom-table wizard requires one |
| Data collection rule (DCR) | `dcr-shoplite-sql` | Receives the collector's 9 SQL internals tables (created by the wizard with the first table) |
| Data collection rule | `dcr-shoplite-ops` | Receives the collector's 7 operations tables |
| Data collection rule | `dcr-shoplite-vm` | Tells the Azure Monitor Agent on the VM what to collect: performance counters, Windows event logs |
| 16 custom tables | `..._CL` | The collector's tables, from `observability/schema.yml` |
| Managed identity | prod-sql-01's system-assigned identity | How the collector signs in to send data: no secret on the VM |

**Cost:** Log Analytics includes **5 GB of ingestion a month free** (per billing account) and **31
days of retention** for these tables. ShopLite's whole volume is a fraction of that (section 9).
DCEs, DCRs, custom tables, the agent, and the managed identity cost nothing themselves.

---

## 1. Log Analytics workspace

Portal search: **Log Analytics workspaces** → **Create**.

| Field | Value |
|---|---|
| Subscription / Resource group | yours / `rg-shoplite-prod` |
| Name | `law-shoplite-prod` |
| Region | North Central US |
| Pricing tier | Pay-as-you-go (the default; the 5 GB/month free allowance applies to it) |
| Tags | `project=atlas`, `env=prod`, `app=shoplite` |

**Review + create** → **Create**. Then, in the new workspace:

**1a. Retention: 31 days** (the included amount; the default is 30). **Usage and estimated costs** →
**Data retention** → **31 days** → **OK**.

**1b. Daily cap: a safety net, not a budget.** **Usage and estimated costs** → **Daily cap** → **ON**,
**0.5 GB/day** → **OK**. Expected use is about 0.02 GB a day (section 9), so the cap never triggers in
normal operation; it only stops a runaway (for example a bug that logs in a loop) from costing real
money. Two things to know: collection stops for the rest of the day once the cap is hit (so alerts go
blind too), and the reset hour is shown but can't be changed.

**1c. Note the workspace's Resource ID**: **Overview** → **JSON View** → `id`. Grafana needs it later.

## 2. Application Insights (workspace-based)

Portal search: **Application Insights** → **Create**.

| Field | Value |
|---|---|
| Resource group | `rg-shoplite-prod` |
| Name | `appi-shoplite-prod` |
| Region | North Central US |
| Resource mode / Log Analytics workspace | Workspace-based, `law-shoplite-prod` |
| Tags | as above |

**Create**. From its **Overview**, copy the **Connection string** into your password manager. API 1.2.0
(B4) reads it from its `.env`. Its data lands in the workspace's `AppRequests`, `AppDependencies`,
`AppExceptions`, ... tables, under the workspace's retention and daily cap. Sampling is configured in
the API (B4), not here.

## 3. Data collection endpoint

Portal search: **Monitor** → **Data Collection Endpoints** → **Create**.

| Field | Value |
|---|---|
| Name | `dce-shoplite-prod` |
| Resource group / Region | `rg-shoplite-prod` / North Central US (must be the workspace's region) |
| Tags | as above |

**Create**. Open it and copy the **Logs Ingestion** URL from **Overview**, e.g.
`https://dce-shoplite-prod-xxxx.northcentralus-1.ingest.monitor.azure.com`. It becomes
`LA_DCE_ENDPOINT` in the collector's `.env`.

## 4. The 16 custom tables (and the two collector DCRs)

The collector's tables are listed in `observability/schema.yml`. Each one is created with the
workspace's table wizard, using two files from **`observability/tables/`** (generated from the schema
by `observability/tools/make_table_files.py`):
- **`<table>.json`**: a sample record, uploaded so the wizard knows the columns;
- **`transformations.kql`**: one section per table, pasted into the wizard. It converts every column
  to its exact type (Microsoft doesn't document how the wizard guesses types from a sample, so we
  don't rely on the guess).

The **first** table of each group also creates its DCR; the others are added to it.

| Group | DCR | Tables (create in this order; the first creates the DCR) |
|---|---|---|
| SQL internals | `dcr-shoplite-sql` | `CollectorHeartbeat_CL`, `SqlWaitStats_CL`, `SqlPerfCounters_CL`, `SqlSessions_CL`, `SqlBlocking_CL`, `SqlXEvents_CL`, `SqlDatabases_CL`, `SqlFileIo_CL`, `SqlTopQueries_CL` |
| Operations | `dcr-shoplite-ops` | `SqlAgentJobRuns_CL`, `SqlBackups_CL`, `OpsEtlRuns_CL`, `OpsRefreshRuns_CL`, `DbaRestoreTests_CL`, `DbaCommandLog_CL`, `VendorFeed_CL` |

**For each table** (it takes a couple of minutes each; do them all in one sitting):

1. Workspace `law-shoplite-prod` → **Tables** → **Create** → **New custom log (DCR-based)**.
2. **Basics:**
   - **Table name:** the name **without** `_CL` (the portal adds it): e.g. `CollectorHeartbeat`.
   - **Data collection rule:** for the first table of a group, **Create a new data collection rule**,
     name `dcr-shoplite-sql` (or `dcr-shoplite-ops`), resource group `rg-shoplite-prod`. For every
     other table, **select the existing DCR** of its group.
   - **Data collection endpoint:** `dce-shoplite-prod` (greyed out once the DCR has one: fine).
3. **Schema and transformation:**
   - **Browse for files** → `observability/tables/<table>_CL.json` (e.g. `CollectorHeartbeat_CL.json`).
   - **Transformation editor** → replace the query with that table's section from
     `transformations.kql` (from `source` to the end of its `project` list) → **Run** → **Apply**.
   - The schema preview must show exactly the columns of that table in `schema.yml`, with the types
     there (`boolean` for bool, `datetime`, `int`, `long`, `real`, `string`).
4. **Review + create** → **Create**.

**If a column type comes out wrong:** a custom table's column type can't be changed after creation.
Delete the table (Tables → `...` → **Delete**), wait a minute, and create it again with the
transformation pasted.

**Afterwards, for each of the two DCRs:** Monitor → **Data Collection Rules** → the DCR → **JSON View** →
copy **`immutableId`** (`dcr-0123abcd...`). They become `LA_DCR_SQL_IMMUTABLE_ID` and
`LA_DCR_OPS_IMMUTABLE_ID` in the collector's `.env`. In the same JSON, `streamDeclarations` should list
one `Custom-<table>` per table of the group (9 and 7).

## 5. The VM's managed identity, allowed to send

A managed identity is an identity Azure gives the VM itself. Code on the VM gets short-lived tokens for
it from a local endpoint, so the collector needs **no secret** to send to Log Analytics.

**5a. Turn it on:** Virtual machines → `prod-sql-01` → **Security** → **Identity** → **System assigned** →
**Status: On** → **Save** → **Yes**. (Step 6 may already have done it: the agent needs it too.)

**5b. Allow it to send, on each collector DCR.** For `dcr-shoplite-sql`, then again for
`dcr-shoplite-ops`: the DCR → **Access control (IAM)** → **Add** → **Add role assignment** →

| Field | Value |
|---|---|
| Role | **Monitoring Metrics Publisher** (it grants `Microsoft.Insights/Telemetry/Write`: sending data, nothing else) |
| Assign access to | Managed identity |
| Members | Virtual machine → `prod-sql-01` |

**Review + assign.** The role is on the two DCRs only, not the resource group: the VM can send to these
two rules and do nothing else in Azure. **Role assignments can take up to 30 minutes** to work; until
then the collector's sends fail with 403 and its records wait in its buffer, which is fine.

## 6. The Azure Monitor Agent and its rule (`dcr-shoplite-vm`)

The agent collects what Windows itself knows: CPU, memory, disk space and latency, and the event logs.
Creating a DCR that targets the VM installs the agent automatically.

Monitor → **Data Collection Rules** → **Create**:

**Basics:** name `dcr-shoplite-vm`, resource group `rg-shoplite-prod`, region North Central US,
**Platform type: Windows**, Data collection endpoint: *(none needed)*. Tags as above.

**Resources:** **Add resources** → `prod-sql-01`. (This installs the Azure Monitor Agent extension and
turns on the system-assigned identity if it's off.)

**Collect and deliver** → **Add data source**, three times:

**6a. Performance counters** → **Custom**, sample rate **60 seconds**, these counters (remove the
defaults first):

```
\Processor(_Total)\% Processor Time
\Memory\Available MBytes
\Memory\% Committed Bytes In Use
\LogicalDisk(C:)\% Free Space
\LogicalDisk(F:)\% Free Space
\LogicalDisk(F:)\Free Megabytes
\LogicalDisk(F:)\Avg. Disk sec/Read
\LogicalDisk(F:)\Avg. Disk sec/Write
\LogicalDisk(F:)\Current Disk Queue Length
\Process(sqlservr)\% Processor Time
\Process(python)\% Processor Time
```

Destination: **Azure Monitor Logs** → `law-shoplite-prod` (they land in the `Perf` table). `F:` is the
data disk (SQL data, logs, backups buffer); the data-disk-full alert uses `% Free Space` on it.
(`python` covers the API and the collector; Windows shows several `python#n` instances.)

**6b. Windows event logs** → **Custom**, these XPath queries:

```
System!*[System[(Level=1 or Level=2 or Level=3)]]
Application!*[System[(Level=1 or Level=2 or Level=3)]]
Application!*[System[Provider[@Name='MSSQLSERVER']]]
```

The first two: critical, error, and warning events from Windows and applications (service crashes,
disk errors). The third: **every** event SQL Server writes to the Application log, at any level.
Destination: `law-shoplite-prod` (the `Event` table).

**Why the third, and not SQL Server's ERRORLOG file:** the phase plan said "SQL Server error log via
the agent". The agent can only read text files in ASCII or UTF-8, and SQL Server writes ERRORLOG in
**UTF-16**, which it doesn't support. SQL Server also writes its error log entries to the Windows
Application log (source `MSSQLSERVER`): startup and shutdown, failed logins (error 18456), serious
errors, backup results. So the Application log carries the same information, in a form the agent
reads. Errors of severity 16 and up also arrive through the collector's Extended Events
(`SqlXEvents_CL`).

**Review + create** → **Create**. Within about 10 minutes the VM → **Extensions + applications** shows
`AzureMonitorWindowsAgent` as **Provisioning succeeded**.

**VM platform metrics need nothing:** CPU, **CPU Credits Remaining**, disk and network metrics are
collected by Azure for free, without the agent. The CPU-credits alert uses them directly.

## 7. Point the collector at it

On the VM, fill the collector's `.env` (server/collector/README.md, "On prod-sql-01", step 3) with:

| Setting | From |
|---|---|
| `LA_DCE_ENDPOINT` | step 3 (the DCE's Logs Ingestion URL) |
| `LA_DCR_SQL_IMMUTABLE_ID` | step 4 (`dcr-shoplite-sql` → JSON View → `immutableId`) |
| `LA_DCR_OPS_IMMUTABLE_ID` | step 4 (`dcr-shoplite-ops` → JSON View → `immutableId`) |
| `MANAGED_IDENTITY_CLIENT_ID` | leave empty (system-assigned identity) |

Then run one cycle by hand (`main.py --once`) and install the service (same README, steps 5–6).

## 8. Check that data arrives

Workspace → **Logs** (switch the editor to **KQL mode**). Data takes a few minutes to appear the first
time; a new custom table can take up to 15 minutes for its first rows.

```kusto
// The collector is alive: one row a minute
CollectorHeartbeat_CL
| top 10 by TimeGenerated desc
| project TimeGenerated, CycleSeconds, QueriesFailed, FailedCollections, SqlOk, LogAnalyticsOk, AdxOk, BufferedRecords
```

```kusto
// Every collector table has rows (event tables only once something happened)
union withsource = TableName *_CL
| where TimeGenerated > ago(1h)
| summarize Rows = count(), Newest = max(TimeGenerated) by TableName
| order by TableName asc
```

```kusto
// The agent: its own heartbeat, counters, and events
Heartbeat | where Computer startswith "prod-sql-01" | top 1 by TimeGenerated desc;
Perf | where TimeGenerated > ago(15m) | summarize Samples = count() by ObjectName, CounterName, InstanceName;
Event | where TimeGenerated > ago(1d) | summarize Events = count() by EventLog, Source, EventLevelName
```

Run each of the three statements in the last block on its own (select it, **Run**).

| Problem | Likely cause |
|---|---|
| No `CollectorHeartbeat_CL` rows, collector log says `send failed ... 403` | The role assignment (5b) isn't there, or hasn't propagated yet (up to 30 minutes) |
| `send failed ... 404`, or "stream" / "InvalidStream" | Wrong DCE URL or immutable ID in `.env`, or that table's stream is missing from its DCR (re-check step 4's JSON View) |
| A table exists but stays empty | Its rows only come when something happens (blocking, job runs, ...), or the table is in the wrong DCR for the collector (see `data_collection_rules` in `schema.yml`) |
| No `Heartbeat`/`Perf`/`Event` | The agent extension isn't provisioned yet, or the VM isn't in `dcr-shoplite-vm`'s Resources |

## 9. Cost settings and checking usage

| Source | Expected volume | Notes |
|---|---|---|
| Collector (16 tables) | about 10 MB per business day | 40,000 rows of about 350 bytes (`schema.yml`, `volume_estimate`) |
| Agent: performance counters | about 3 MB per day | 11 counters, once a minute |
| Agent: event logs | under 1 MB per day | warnings and errors only, plus SQL Server's entries |
| Application Insights | a few MB per day | with the sampling set in B4 |
| **Total** | **about 0.5 GB a month** | against 5 GB free |

The VM is off nights and weekends, so these are business-day numbers. Check real usage weekly at first:

```kusto
Usage
| where TimeGenerated > ago(30d)
| summarize GB = round(sum(Quantity) / 1024, 3) by DataType
| order by GB desc
```

Also: workspace → **Usage and estimated costs** shows the month so far. Record the real numbers in the
runbook (B7).

## 10. Checklist: what should exist when you're done

- [ ] `law-shoplite-prod`: retention 31 days, daily cap 0.5 GB/day
- [ ] `appi-shoplite-prod`: workspace-based on `law-shoplite-prod`; connection string saved
- [ ] `dce-shoplite-prod`: Logs Ingestion URL noted
- [ ] 16 custom tables, columns and types as in `schema.yml`
- [ ] `dcr-shoplite-sql` with 9 streams, `dcr-shoplite-ops` with 7; immutable IDs noted
- [ ] prod-sql-01's system-assigned identity **On**, with **Monitoring Metrics Publisher** on both collector DCRs (and nothing else)
- [ ] `dcr-shoplite-vm` targeting prod-sql-01: 11 performance counters, 3 event log queries; agent extension provisioned
- [ ] Collector `.env` filled in, service running, heartbeat arriving every minute
- [ ] `Perf`, `Event`, and agent `Heartbeat` rows arriving
- [ ] Usage query run once; volumes recorded

Next: the action group and alert rules (`observability/alerts/README.md`).
