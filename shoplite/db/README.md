# shoplite/db

Everything needed to build **SalesDB**, ShopLite's SQL Server database, and fill it with realistic
data. Built in Phase 1. Every incident Atlas diagnoses later starts with the schema, data, and
indexes defined here.

> **Warning:** `apply_schema.py` **drops and recreates SalesDB**. Any data in it is lost. That is
> intended for local development, where you rebuild from scratch whenever you like. Never point it
> at production.

## Files

| File | What it does |
|---|---|
| `001_create_database.sql` | Drops and recreates SalesDB; sets `SIMPLE` recovery and turns on Query Store |
| `002_tables.sql` | Creates the `dbo` tables and the `vendor` schema with `vendor.supplier_prices`. `seed.py` fills it with **placeholder** supplier rows; since Phase 4 the real rows come from the vendor ETL (`server/etl/`). After a local rebuild, re-run `server/etl/sql` (001, 002, 900) before the next ETL run. |
| `003_indexes.sql` | Indexes for the queries the API runs, each commented with the query it serves |
| `004_logins.sql` | Creates the `shoplite_app` login and user with least-privilege permissions |
| `005_report_procedures.sql` | Stored procedures behind the admin sales and top-products reports |
| `apply_schema.py` | Runs the numbered scripts in order as `sa`, stopping on the first error |
| `seed.py` | Generates realistic data (skew, seasonality, fixed random seed) and bulk-loads it |
| `requirements.txt` | Python packages for `apply_schema.py` and `seed.py` |
| `queries/verify.sql` | Sanity checks: row counts, best sellers, orders per month, busiest customers |

## Logins

- **`sa`** is used only by `apply_schema.py` and `seed.py`, to build the schema and load data.
- **`shoplite_app`** is what the API uses. It can read and write data and execute stored procedures,
  and nothing else: no creating tables, no changing settings. Its password is `SHOPLITE_APP_PASSWORD`
  in the root `.env`.

## How to run

Run from the repo root, with the Phase 0 containers healthy (`docker compose ps`) and the root
virtual environment active (`source .venv/bin/activate`).

1. Add `SHOPLITE_APP_PASSWORD` to the root `.env`. Same rules as `SA_PASSWORD`: long, mixed case,
   digits, and only the safe symbols listed in `.env.example`.

2. Install the database tools:

   ```bash
   pip install -r shoplite/db/requirements.txt
   ```

3. Build the schema (drops and recreates SalesDB):

   ```bash
   python shoplite/db/apply_schema.py
   ```

4. Load the data with the local defaults. Under Rosetta this takes a while; progress is printed.

   ```bash
   python shoplite/db/seed.py
   ```

   Parameters (all optional):

   | Option | Local default | Production (Phase 2) |
   |---|---|---|
   | `--products` | 5000 | 5000 |
   | `--customers` | 50000 | 50000 |
   | `--orders` | 200000 | 1000000 |
   | `--years` (order history length) | 2 | 2 |
   | `--seed` (random seed) | 42 | 42 |
   | `--end-date` (last day of history, `YYYY-MM-DD`) | today (UTC) | today (UTC) |

   The same parameters always produce the same data. Because `--end-date` defaults to today, the
   history always ends "now"; pass an explicit `--end-date` to reproduce an identical dataset on a
   different day. `seed.py` refuses to run if SalesDB already has data; run `apply_schema.py` again
   to start fresh.

5. In VS Code's SQL Server extension, connect to `localhost,1433`, open SalesDB, and run
   `queries/verify.sql`. Best sellers should stand out, months should vary with the seasons, and a
   few customers should have many orders.
