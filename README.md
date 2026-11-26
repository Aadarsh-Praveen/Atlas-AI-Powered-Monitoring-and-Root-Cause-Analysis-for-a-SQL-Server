# Atlas: AI-Powered Monitoring and Root-Cause Analysis for a SQL Server

Atlas is a team of AI agents built on Microsoft Foundry that watches a production SQL Server around
the clock. When something goes wrong, Atlas investigates on its own, records every debugging step,
explains the root cause with evidence, and recommends a fix. Atlas never changes the server: a human
reviews the diagnosis, applies the fix, and Atlas verifies that it worked. The monitored system is
**ShopLite**, a small e-commerce app (Next.js storefront, FastAPI backend, SQL Server 2022) running
under continuous simulated traffic.

For the full architecture, key decisions, and roadmap, see `docs/00-project-overview.md`. Each phase
has its own file in `docs/`, and `docs/build-log.md` records every file added. **`docs/` is kept on
the owner's machine only** (it's in `.gitignore`), so those files aren't on GitHub.

## Repository layout

```
(repo root)
├── CLAUDE.md               rules for Claude Code
├── docker-compose.yml      local SQL Server and Postgres for development
├── .env.example            template for the root .env (secrets stay local)
├── docs/                   overview, phase files, build log (local only, not in Git)
├── infra/                  Bicep templates
├── shoplite/
│   ├── db/                 schema and seed data
│   ├── api/                FastAPI backend
│   ├── web/                Next.js storefront
│   └── traffic/            Playwright personas, Locust
├── server/                 SQL Agent jobs, Extended Events, collector, vendor ETL
├── vendor/                 the vendor's Postgres: schema, seed, daily changes
├── observability/          monitoring: table schema, ADX, alerts, Grafana dashboards
├── incidents/              the incident injector (never readable by Atlas)
├── atlas/
│   ├── mcp/                read-only MCP servers
│   ├── agents/             agent definitions and workflows
│   ├── api/                Atlas API
│   └── dashboard/          Atlas dashboard (Next.js)
├── knowledge/              runbooks and documents for grounding
├── evals/                  evaluation datasets and evaluators
├── tools/                  small helper scripts
└── .github/workflows/      CI/CD and scheduled traffic
```

Each folder has a short `README.md` saying what it will hold and in which phase it's built.

## Getting started locally on a Mac

Locally, SQL Server 2022 and Postgres 16 run in Docker. Your Mac is the development environment;
production (from Phase 2) is an Azure VM.

**Prerequisites:** the tools in Part A of [docs/phase-0-setup.md](docs/phase-0-setup.md): Docker
Desktop, Python 3.12, and Microsoft ODBC Driver 18 for SQL Server. On Apple Silicon, turn on
**"Use Rosetta for x86/amd64 emulation"** in Docker Desktop settings, because the SQL Server image
is built for Intel processors.

### 1. Create your `.env`

```bash
cp .env.example .env
```

Open `.env` and replace each `replace-me` with a real password from your password manager.
`SA_PASSWORD` must meet SQL Server's complexity rules (see the comments in `.env.example`).
`.env` is ignored by Git and must never be committed.

**This root `.env` is the only settings file you need locally.** Every component reads settings in
layers (`tools/envconfig.py`): real environment variables first, then the component's own `.env` if it
has one, then this root `.env`. A value set higher up always wins. Each login has one variable here,
and components build their connection strings and Postgres URLs from these parts (encoding special
characters). A component's own `.env` is only needed **on the VM**, or to override one value locally.

### Settings: which component uses what

| Root variable | Docker | DB scripts¹ | API | ETL | Collector | Injector | Vendor scripts² | Shoppers, Locust |
|---|---|---|---|---|---|---|---|---|
| `SA_PASSWORD`, `SQL_ADMIN_USER` | ✓ | ✓ |  |  |  | ✓ (local "admin" sessions) |  |  |
| `SQL_TRUST_SERVER_CERTIFICATE` |  |  | ✓ (as `DB_TRUST_SERVER_CERTIFICATE`) | ✓ (as `SALESDB_TRUST_SERVER_CERTIFICATE`) | ✓ | ✓ |  |  |
| `SHOPLITE_APP_PASSWORD` |  | ✓ (creates the login) | ✓ (as `DB_PASSWORD`) |  |  | ✓ (app-like sessions) |  |  |
| `SHOPLITE_ETL_PASSWORD` |  | ✓ (ETL login script) |  | ✓ (as `SALESDB_PASSWORD`) |  |  |  |  |
| `SHOPLITE_MONITOR_PASSWORD` |  | ✓ (monitor login script) |  |  | ✓ (as `SQL_PASSWORD`) |  |  |  |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | ✓ |  |  | ✓ (database name) | ✓ (database name) | ✓ (vendor admin) | ✓ |  |
| `SHOPLITE_READER_PASSWORD` |  | ✓ (vendor roles) |  | ✓ (reader URL) | ✓ (reader URL) |  |  |  |
| `VENDOR_ACTIVITY_PASSWORD` |  | ✓ (vendor roles) |  |  |  |  | ✓ (`activity.py`) |  |
| `VENDOR_HOST`, `VENDOR_PORT` |  |  |  | ✓ | ✓ | ✓ | ✓ |  |
| `ADMIN_TOKEN` |  |  | ✓ |  |  | ✓ |  | ✓ (as `SHOPLITE_ADMIN_TOKEN`) |
| `STOREFRONT_URL`, `API_URL` |  |  |  |  |  |  |  | ✓ |
| `GRAFANA_*` (optional) | | | | | | | | |

`GRAFANA_*` is read only by `observability/tools/make_dashboards.py` (import-ready dashboards with your
values, `observability/grafana/README.md`).

¹ `shoplite/db/apply_schema.py` and `seed.py`, `tools/check_local.py` (these three only ever read the
root `.env`), and the login scripts you run with sqlcmd (pass the same value as in this file).
² `vendor/seed.py`, `vendor/activity.py`.

Every other setting has a local default (hosts `localhost`, ports, database and login names, timeouts),
so locally the root `.env` is all you need. The VM templates still set every value explicitly.

"as `DB_PASSWORD`" means the component's own name for it: if its own `.env` sets that name, it wins;
if not, it uses the root variable. That's how the **VM** works: each service (API, ETL, collector)
runs as its own account and has its own `.env`, locked by ACL, with `LOAD_ROOT_ENV=false`, so the root
`.env` (admin passwords, Administrators only) is never read by a service, even when you run one by hand.
Full URLs given explicitly (Neon's `VENDOR_READER_URL`, the GitHub secret `VENDOR_ACTIVITY_URL`) are
used exactly as given.

### 2. Start the databases

```bash
docker compose up -d
docker compose ps
```

Repeat `docker compose ps` until both `shoplite-sql` and `vendor-pg` show as `healthy`. SQL Server
can take a minute or two the first time, longer under Rosetta.

| Container | Database | Connect to |
|---|---|---|
| `shoplite-sql` | SQL Server 2022 Developer | `localhost,1433`, user `sa` |
| `vendor-pg` | Postgres 16 (vendor stand-in) | `localhost:5433`, user from `POSTGRES_USER` |

Both ports are bound to `127.0.0.1`, so only your Mac can reach them. Postgres is published on
**5433** rather than its usual 5432 so it doesn't clash with a Postgres installed directly on the
Mac; inside the container it still listens on 5432. Point your Postgres client at port 5433, or you
will be talking to the wrong server.

### 3. Check the connections from Python

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r tools/requirements.txt
python tools/check_local.py
```

Both databases should report `[OK]` with their version and server time. If one fails, the script
prints the most likely cause.

### Stopping and troubleshooting

```bash
docker compose down                  # stop the containers; data is kept in named volumes
docker compose logs shoplite-sql     # why SQL Server failed (often a weak SA_PASSWORD)
docker compose logs vendor-pg        # why Postgres failed
```

`docker compose down -v` also deletes the volumes, which erases all local database data.

## Production (Phase 2)

ShopLite runs in Azure and on Vercel, the way a small company would run it:

```
Shopper's browser ──HTTPS──▶ Vercel: storefront (shoplite/web), https://shoplite-aadarsh.vercel.app
        │
        └──HTTPS──▶ shoplite-aadarsh.northcentralus.cloudapp.azure.com
                      Azure VM prod-sql-01 (Windows Server 2022, B2as_v2, UTC, North Central US)
                      ├─ Caddy :443/:80   Let's Encrypt certificate, forwards to the API
                      ├─ ShopLite API     Windows service, one Uvicorn process, 127.0.0.1:8000
                      └─ SQL Server 2022  Developer, local connections only, SalesDB (1,000,000 orders)
```

Only 443 and 80 are open to the internet, and RDP (3389) only from the owner's IP. **SQL Server
(1433) is never reachable from outside the VM.** The VM is deallocated every evening by an
auto-shutdown schedule.

| Guide | What it covers |
|---|---|
| [infra/README.md](infra/README.md) | Bicep: deploy the resource group's contents, what each resource is, billing, teardown |
| [server/vm-setup/README.md](server/vm-setup/README.md) | Preparing the VM: data disk, SQL Server settings, prerequisites, firewall, building and seeding SalesDB |
| [shoplite/api/deploy/README.md](shoplite/api/deploy/README.md) | Installing the API and Caddy services, later deploys with `deploy.ps1`, rollback |
| [shoplite/web/README.md](shoplite/web/README.md) | The storefront, including its Vercel setup |
| [knowledge/runbooks/shoplite-production.md](knowledge/runbooks/shoplite-production.md) | Operating production: ports, services, logs, health, the "site is down" checklist |

Production uses **new** passwords and tokens, stored only in the VM's `.env` files and the owner's
password manager. Never run the API smoke tests against production.
