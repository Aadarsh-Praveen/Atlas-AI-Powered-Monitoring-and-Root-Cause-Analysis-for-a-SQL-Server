# ADX and Grafana setup (Phase 5, Part C2)

The long-term history store and the dashboards. Neither needs the VM: you can do all of this from the
Mac before production monitoring exists. Recent data and alerts stay in Log Analytics
(`observability/azure-setup.md`); ADX keeps **12 months**, Grafana only **shows** (it stores nothing).

```
collector ──(app registration: ingestor)──▶ ADX free cluster, database shoplite (12 months)
                                                  ▲
Grafana Cloud ──(app registration: read only)─────┤
                                                  └──▶ also Log Analytics (recent data, alerts)
```

| What | Name | Notes |
|---|---|---|
| ADX free cluster | your choice, e.g. `shoplite` | One per account, free, no SLA; **the term is one year**, which Microsoft may renew (record the date in the runbook) |
| ADX database | `shoplite` | 16 tables, 5 hourly rollups, 365-day retention (`observability/adx/001_shoplite_database.kql`) |
| ADX database (test only) | `shoplite_dev` | For testing the collector's sign-in from the Mac without mixing test data into production history |
| App registration | `shoplite-collector-adx` | The collector's identity for ADX: **ingestor** on `shoplite` (and `shoplite_dev`) only |
| App registration | `shoplite-grafana-reader` | Grafana's identity: **viewer** on `shoplite`, **Log Analytics Reader** on `law-shoplite-prod`, **Monitoring Reader** on `prod-sql-01` (VM metrics), nothing else |
| Grafana Cloud stack | your choice, e.g. `shoplite` | Free tier |

**Why app registrations:** the free cluster doesn't support managed identities, so the collector signs
in as an app registration with a secret (in its locked `.env` only). Grafana Cloud runs outside Azure,
so it needs one too. Two separate identities, each with exactly the rights it needs: the collector can
add data but not read it back; Grafana can read but not change anything.

**Secrets expire.** Both client secrets are created with a 12-month expiry. Record both dates in the
runbook (B7), next to the backup SAS expiry; an expired secret silently stops ADX ingestion (the
heartbeat shows `AdxOk = false`) or empties every Grafana panel.

---

## 1. The ADX free cluster and databases

1. Go to **https://dataexplorer.azure.com/freecluster** and sign in with the account you use for Azure.
2. **Create cluster**: cluster name `shoplite`, **database name `shoplite`**, location: the US region
   offered closest to North Central US. **Create** (a minute or two).
3. On **My cluster**, copy two URIs into your password manager notes (they aren't secrets, but you'll
   need them):
   - **Cluster URI** (for queries and Grafana), e.g. `https://kvc-xxxx.northcentralus.kusto.windows.net`
   - **Data ingestion URI** (for the collector), the same with `ingest-` in front, e.g.
     `https://ingest-kvc-xxxx.northcentralus.kusto.windows.net`
4. **Create a second database**, `shoplite_dev` (My cluster → **Create database**). The free cluster
   allows up to 10.

## 2. Tables and rollups

In the ADX web UI (**Query**), select the database **`shoplite`** in the left pane, open
`observability/adx/001_shoplite_database.kql`, and follow its header:
- **Part 1:** select from `.execute database script` to the end of Part 1 and **Run** (the web UI only
  runs the selection; without selecting, it runs one block and stops at the first blank line).
- **Part 2:** select and run each `.create ifnotexists materialized-view` on its own.

Then for **`shoplite_dev`**: select it in the left pane, and run Part 1 again with one change: the
first command's database name, `.alter-merge database shoplite_dev policy retention softdelete = 7d`
(a week is plenty for test data). Part 2 isn't needed there.

**Check:**

```kusto
.show tables                                        // 16 tables, in each database
.show materialized-views | project Name, IsHealthy  // 5 in shoplite, all healthy
.show database shoplite policy retention            // SoftDeletePeriod 365.00:00:00
```

## 3. The collector's app registration (ingestor)

**3a. Create it.** Azure portal → **Microsoft Entra ID** (your Default Directory) → **App registrations**
→ **New registration**: name `shoplite-collector-adx`, **Accounts in this organizational directory
only**, no redirect URI → **Register**. From its **Overview**, note the **Application (client) ID** and
the **Directory (tenant) ID**.

**3b. A secret.** **Certificates & secrets** → **New client secret** → description `collector`,
expires **12 months** → **Add**. **Copy the Value now** (it's shown once) into your password manager.
Note the expiry date.

**3c. Give it the ingestor role**, in the ADX web UI, on each database (replace the two IDs):

```kusto
.add database shoplite ingestors ('aadapp=<application-id>;<tenant-id>') 'shoplite collector'
.add database shoplite_dev ingestors ('aadapp=<application-id>;<tenant-id>') 'shoplite collector (test)'
.show database shoplite principals                  // the app is listed with role "Database Ingestor"
```

**Ingestor** can add data to the database's tables and nothing else: no queries, no schema changes.

**3d. Test it from the Mac, against `shoplite_dev`.** Microsoft's free-cluster page doesn't say
whether an app registration can ingest there, so prove it before the VM depends on it. In the
collector's local `.env`, set the ADX values with **`ADX_DATABASE=shoplite_dev`** (leave the Log
Analytics values empty), then one real cycle (not `--print`):

```bash
cd server/collector && .venv/bin/python main.py --once && cd -
```

The log must show no `adx: send failed` line. Queued ingestion loads data in batches, so wait about 5
minutes, then in the ADX web UI with `shoplite_dev` selected:

```kusto
CollectorHeartbeat_CL | take 5
union withsource = TableName *_CL | summarize Rows = count() by TableName
.show ingestion failures | where FailedOn > ago(1h)
```

Rows in the tables and no ingestion failures: the app registration works. Then set your local `.env`
back (empty the ADX values, or keep `shoplite_dev` for future tests; **never** point the local collector
at `shoplite`). The production `.env` on the VM uses `ADX_DATABASE=shoplite`.

| Problem | Likely cause |
|---|---|
| `send failed ... 401` or `AADSTS7000215` | Wrong secret value (the secret's ID was copied instead of its Value) |
| `send failed ... 403`, "Principal ... is not authorized" | The `.add database ... ingestors` command wasn't run for that database |
| No `send failed`, but no rows after 10 minutes | `.show ingestion failures` says why: usually a mapping mismatch (`collector_json` missing or stale: re-run Part 1) |
| `AADSTS700016` | Wrong application ID or tenant ID |

If an app registration turns out not to be allowed on the free cluster at all, stop and tell Claude
Code: the collector's ADX sender then needs another approach, and the phase file says to choose
together.

## 4. Grafana's read-only app registration

**4a. Create it:** App registrations → **New registration** → `shoplite-grafana-reader`, single tenant
→ **Register**. Note its application and tenant IDs. **Certificates & secrets** → new secret
`grafana`, **12 months**; copy the Value; note the expiry.

**4b. Read access to ADX** (ADX web UI, database `shoplite`):

```kusto
.add database shoplite viewers ('aadapp=<grafana-application-id>;<tenant-id>') 'grafana (read only)'
```

**Viewer** can query and see the schema; it can't add, change, or delete anything.

**4c. Read access to Log Analytics**, on the workspace only: Azure portal → `law-shoplite-prod` →
**Access control (IAM)** → **Add role assignment** → role **Log Analytics Reader** → **User, group, or
service principal** → select `shoplite-grafana-reader` → **Review + assign**. (Read-only, and only this
workspace: Grafana can't see or change anything else in the subscription.)

**4d. Read access to the VM's metrics** (the CPU credits and CPU charts, added in B5): Virtual machines
→ `prod-sql-01` → **Access control (IAM)** → **Add role assignment** → role **Monitoring Reader** →
select `shoplite-grafana-reader` → **Review + assign**. Monitoring Reader can read monitoring data and
settings only: it can't start, stop, or change the VM.

## 5. Grafana Cloud and its two data sources

**5a. The stack.** Sign up at **https://grafana.com** (free tier) and create a stack, e.g. `shoplite`,
in a US region. Open it (`https://<stack>.grafana.net`).

**5b. Azure Data Explorer data source.** **Connections** → **Add new connection** → search **Azure Data
Explorer** → install the plugin if asked → **Add new data source**:

| Field | Value |
|---|---|
| Name | `ADX shoplite` |
| Cluster URL | the **Cluster URI** from step 1 (not the ingestion URI) |
| Authentication | **App Registration**: tenant ID, client ID, client secret of `shoplite-grafana-reader` |
| Default database | `shoplite` |

**Save & test** → "Success".

**5c. Azure Monitor data source.** **Connections** → **Data sources** → **Add data source** → **Azure
Monitor**:

| Field | Value |
|---|---|
| Name | `Azure Monitor shoplite` |
| Authentication | **App Registration**: the same tenant ID, client ID, client secret |
| Default subscription | the one holding `rg-shoplite-prod` |

**Save & test** → it lists the subscription; "Successfully queried" for Log Analytics.

**5d. Check both with one query each** (**Explore**):
- `ADX shoplite`: `CollectorHeartbeat_CL | summarize count() by bin(TimeGenerated, 1h)` (data once the
  production collector sends; until then the table exists and is empty).
- `Azure Monitor shoplite` → **Logs** → resource `law-shoplite-prod`: `Heartbeat | take 5`.

The dashboards come in B5 (as JSON in `observability/grafana/`), using these two data source names.
Grafana stores the client secret encrypted; it isn't in the repo or in any dashboard JSON.

## 6. Checklist

- [ ] ADX free cluster created; cluster URI and ingestion URI noted; one-year term date recorded
- [ ] `shoplite`: 16 tables, 5 healthy materialized views, 365-day retention
- [ ] `shoplite_dev`: 16 tables, 7-day retention
- [ ] `shoplite-collector-adx`: ingestor on both databases; secret saved; expiry noted
- [ ] Local test against `shoplite_dev`: rows arrived, no ingestion failures
- [ ] `shoplite-grafana-reader`: viewer on `shoplite`, Log Analytics Reader on the workspace, Monitoring Reader on `prod-sql-01` (nothing else); secret saved; expiry noted
- [ ] Grafana stack with `ADX shoplite` and `Azure Monitor shoplite`, both tested
