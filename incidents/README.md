# incidents

The **incident injector**: scripts that cause realistic problems in ShopLite on purpose, for a limited
time, then put everything back, while recording a hidden **ground truth** (what was injected, the true
root cause, the correct fix). Monitoring (Phase 5) proves it notices them; Atlas (Phase 6 on) has to
diagnose them; Phase 10 scores Atlas against the ground truth.

> **This folder is an answer key.** Each incident's `root_cause`, `correct_fix`, and `expected_signals`
> are written in `scenarios/`. **Atlas never gets access to `incidents/`**: its knowledge sources are
> limited to `knowledge/` (docs/phase-5-monitoring.md, A5). The ground truth itself lives in a separate
> storage account no Atlas identity can read.

## The incidents

| Incident | Runs on | Minutes (max) | What happens |
|---|---|---|---|
| `blocking-chain` | VM | 15 (30) | A forgotten SSMS transaction locks hot inventory rows; shoppers queue and time out |
| `missing-index` | VM | 20 (45) | An index product pages rely on is disabled; product pages slow down |
| `plan-regression` | VM | 20 (40) | A forced Query Store plan, made for one day, is used for two-year reports |
| `deadlock-storm` | VM | 15 (30) | A new two-worker "inventory sync" job deadlocks with itself, every round |
| `log-growth` | VM | 30 (45) | A long open transaction stops log reuse; the log fills (capped at 85% and 1 GB of growth) |
| `cpu-exhaustion` | Mac or VM | 90 (240) | An aggressive crawler drains the burstable VM's CPU credits |
| `vendor-schema-change` | Mac | 45 (120) | The vendor renames `supply.prices.unit_cost`; the ETL refuses the feed |
| `stale-vendor-feed` | Mac | until reverted | The vendor's daily updates stop; after 36 hours the ETL refuses the feed |
| `api-fault` | VM | 20 (45) | Feature flag `search_ranking_v2` goes on: a slow, failing dependency |
| `failed-job` | VM | 15 (30) | The ReportingDB refresh job is edited to call a procedure that doesn't exist |

`python incidents/injector.py list` prints the same from the code.

## Safety rules (enforced by `framework.py`)

- **Start, revert, maximum duration, dry run.** Every incident runs in the foreground and reverts at
  its deadline or on Ctrl+C (`stale-vendor-feed`, which lasts days, starts and exits, and is reverted by
  hand).
- **Production guards:** weekdays only; start at **13:30 UTC or later** (the business-hours suppression
  would hide the alert emails before that); finish by **22:30 UTC** (the VM deallocates at 23:00); for
  incidents that affect shoppers, shopper traffic must be active (an API session ran a request in the
  last 5 minutes); one incident at a time.
- **Never against data it can't restore:** blocking and deadlocks update rows to their own values and
  roll back; log growth works on the staging table and rolls back; the index is rebuilt; the column is
  renamed back; the flag and the job step are restored.
- **No ground truth, no incident:** the start record is written before anything changes.
- **No giveaway:** realistic program names and logins (SSMS, SQLCMD, an "inventory sync" job as
  `shoplite_app`, a crawler's User-Agent), never "injector".
- **Cleanup after a crash:** `status` checks the live system for persistent changes (a disabled index,
  a forced plan, the renamed column, the flag, the broken job step, stale vendor prices), and `revert`
  removes them even if the state file is lost. If `status` can't check an incident (no connection, a
  missing password), it says **Result UNKNOWN**, names the incidents, and exits with code 3, never
  "No persistent incident changes found". Its first line shows the SQL Server login and certificate
  trust it used.

---

## Setup

### Settings: the root `.env` locally, one file per production copy

| Copy | Where | Runs | Settings |
|---|---|---|---|
| Local | your Mac | everything, against Docker SQL Server, vendor-pg, and the local API | **no file**: the root `.env` (admin from `SQL_ADMIN_USER`/`SA_PASSWORD`, `sa` if the user is empty: Windows authentication only in the VM's production copy; certificate trust from `SQL_TRUST_SERVER_CERTIFICATE`, default true; app password and admin token from `SHOPLITE_APP_PASSWORD`/`ADMIN_TOKEN`, vendor admin URL built from `POSTGRES_*`, API at `API_URL`) |
| VM | prod-sql-01 | the SQL Server, API, and job incidents (and `cpu-exhaustion`, though the Mac is better) | `incidents\.env` from the template: `LOAD_ROOT_ENV=false`, `INJECTOR_ENVIRONMENT=prod`, `INJECTOR_HOST=vm`, SQL admin **empty** (Windows authentication), the API's `.env` path and restart command, `GROUND_TRUTH_SAS_URL` |
| Mac, production vendor | your Mac | `vendor-schema-change`, `stale-vendor-feed`, `cpu-exhaustion` | **`incidents/.env.prod`** (a separate file, so local runs never pick it up), used with `--env-file incidents/.env.prod`: `LOAD_ROOT_ENV=false`, `INJECTOR_ENVIRONMENT=prod`, `INJECTOR_HOST=mac`, `VENDOR_ADMIN_URL` (Neon's owner URL: it never goes on the VM), `API_BASE_URL` = the public HTTPS address, `GROUND_TRUTH_SAS_URL` |

`LOAD_ROOT_ENV=false` matters most on the Mac: your root `.env` holds the LOCAL `sa` and vendor
passwords, which must never mix into a production run.

```bash
python3 -m venv incidents/.venv
incidents/.venv/bin/pip install -r incidents/requirements.txt
# Local: nothing to copy (the root .env). Production vendor copy on the Mac:
cp incidents/.env.example incidents/.env.prod     # fill in the production values
python incidents/injector.py --env-file incidents/.env.prod status
```

On the VM (PowerShell as Administrator, from `C:\atlas`), the same with `py -3.12 -m venv
incidents\.venv`, `incidents\.venv\Scripts\pip install ...`, and lock the file:
`icacls C:\atlas\incidents\.env /inheritance:r /grant:r "Administrators:F" "SYSTEM:F"`.

### The ground-truth storage account

**Its own storage account, separate from the backup account** (`stshoplite…`): Atlas may later get
read access to the backup account to investigate backup failures, and the answers must never be one
role assignment away. Put it in **its own resource group** too, so no role granted on
`rg-shoplite-prod` can ever reach it. Create it by hand in the portal:

1. **Resource groups** → **Create**: `rg-shoplite-groundtruth`, North Central US, tags `project=atlas`,
   `env=prod`, `app=shoplite`.
2. **Storage accounts** → **Create**: resource group `rg-shoplite-groundtruth`, name e.g.
   `stshopgt<something unique>`, region North Central US, **Standard**, **LRS**. **Advanced:** require
   secure transfer on, **allow blob anonymous access off**, minimum TLS 1.2. **Create.**
3. In the account: **Containers** → **+ Container**: name **`incident-ground-truth`**, anonymous access
   **Private**.
4. The container → **⋯** → **Generate SAS**: signing method Account key (note **which key**),
   permissions **Create** and **Write only** (not Read, not List, not Delete), expiry 12 months, HTTPS
   only → **Generate** → copy the **Blob SAS URL**. That's `GROUND_TRUTH_SAS_URL` in the VM and Mac
   copies' `.env`. Record its expiry in the runbook.
5. **Never** give any Atlas identity any role on this account or resource group (Phase 6 onward).

It costs cents: a few KB per incident, no lifecycle rule (the answers are kept). **You** read the
ground truth in the portal (container → the date folder → a `-start.json` / `-end.json` → Download),
with your own account; the SAS can't read, by design.

---

## Running an incident

```bash
python incidents/injector.py list
python incidents/injector.py start blocking-chain --dry-run      # always first: what it would do, the ground truth
python incidents/injector.py start blocking-chain                # for real; Ctrl+C reverts early
python incidents/injector.py status                              # anything left over?
python incidents/injector.py revert                              # clean up the active (or a named) incident
```

`--minutes N` changes the duration (up to the incident's maximum); `--skip-traffic-check` is for local
testing without Locust.

### Locally first (Part C1, step 2)

For each incident: a dry run, then a real run, while the collector runs locally with `--print` in
another terminal (server/collector/README.md), and watch its output change (blocking rows, deadlock
events, a forced plan, log percentage, a failed job). The vendor incidents use the local vendor-pg; the
API fault needs your local API running (it asks you to restart it).

### Notes per incident

- **`plan-regression`:** needs the sales report's query in Query Store (it runs the report once through
  the API if needed).
- **`log-growth`:** refuses to overlap the 14:00 ETL in production; its rollback at the end can take a
  minute.
- **`cpu-exhaustion`:** run it from the Mac against the public address for realism. Credits drain
  slowly (hours for a full balance): latency and CPU show within minutes; the CPU-credits alert fires
  only when the balance reaches 30.
- **`vendor-schema-change`:** the ETL runs at 14:00; let the incident span it, or start the ETL job by
  hand on the VM while it's active. Revert, then run the ETL job again to load today's feed.
- **`stale-vendor-feed`:** a multi-day incident; pause on a Thursday after 12:00 UTC and the ETL fails on
  Monday. Revert when you've seen the alerts.
- **`api-fault`:** restarts the API twice (on and off), like two deployments.
- **`failed-job`:** starts the job itself, so the failure happens within minutes; revert runs it again
  so ReportingDB catches up.

## First game day (Part C4)

Three incidents, one at a time, on a weekday afternoon, with the procedure in the runbook ("Game day"):

1. **`blocking-chain`** (15 min) → expect the blocking alert within about 5–10 minutes.
2. **`missing-index`** (20 min) → expect the API latency alert within about 15–25 minutes.
3. **`vendor-schema-change`** (from the Mac, spanning a hand-started ETL run) → expect the ETL-failed
   and job-failed alerts within about 15–30 minutes of the ETL run.

For each, note the start time, when the email arrived, what the dashboards showed, and check that two
ground-truth files (`-start.json`, `-end.json`) exist in the container. Record it in
**`game-days.md`** (here, not in the runbook: what was injected when is part of the answer key).
