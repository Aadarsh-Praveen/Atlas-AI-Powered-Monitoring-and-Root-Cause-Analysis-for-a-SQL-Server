# server

Scripts that run on the SQL Server VM (and, for testing, against the local Docker SQL Server), plus
the DMV collector that feeds Log Analytics (Phase 5).

| Folder | Phase | What |
|---|---|---|
| `vm-setup/` | 2 | Preparing `prod-sql-01`: data disk, SQL Server settings, prerequisites |
| `etl/` | 4 | The vendor ETL: SQL objects, the `shoplite_etl` login, contract validation, `run_etl.py`, tests |
| `reporting/` | 4 | ReportingDB: database, tables, refresh procedure, verification queries |
| `backups/` | 4 | The Blob Storage credential, FULL recovery, and the first full backup |
| `jobs/` | 4 | SQL Agent jobs, server settings, the Extended Events session, the DBA database, the restore test, and the one-page schedule (`jobs/schedule.md`) |

Order for a fresh server: `vm-setup` → `etl` → `reporting` → `backups` → `jobs`. Each folder's README
has the exact commands, locally and on the VM; operating it all day to day is in
`knowledge/runbooks/shoplite-production.md`.
