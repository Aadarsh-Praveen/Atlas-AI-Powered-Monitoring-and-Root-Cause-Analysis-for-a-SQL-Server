# vendor

"The vendor": an external supplier network with its own Postgres database, outside Azure. It changes
every day (prices, stock, new and discontinued items), and ShopLite reads it each weekday with a
**read-only** login in its nightly ETL (`server/etl/`, Phase 4 B2).

| Where | Database |
|---|---|
| Production | **Neon** (free managed Postgres), US East |
| Local | the `vendor-pg` Docker container on `localhost:5433` (from Phase 0) |

You play both roles, but the boundary is real: ShopLite's only access is the `shoplite_reader` login,
limited to the shared tables. The vendor admin connection never leaves your Mac.

## Files

| File | What it is |
|---|---|
| `sql/001_schema.sql` | Schema, triggers, and the two roles. Safe to re-run; re-running also resets the role passwords. |
| `seed.py` | Suppliers and a catalog that maps to ShopLite's 5,000 products (as the admin) |
| `activity.py` | One "vendor day" of changes (as `vendor_activity`) |
| `tests/test_pg_url.py` | Tests for the URLs the scripts build (user and password percent-encoded by `tools/envconfig.py`, so `@`, `#`, `%` in a password work), no database needed: `python -m pytest vendor/tests -v` |
| `requirements.txt` | `psycopg` 3, plus ShopLite's database packages (the seed uses ShopLite's product generator) |
| `../knowledge/contracts/vendor-supplier-feed.yml` | The data contract: exactly what ShopLite depends on |
| `../.github/workflows/vendor-day.yml` | Runs `activity.py` every day at 12:00 UTC |

## What's in the database

**Schema `supply`: the tables shared with ShopLite**

| Table | Contents |
|---|---|
| `suppliers` | 6 suppliers (code, name, active) |
| `catalog_items` | Each supplier's SKU, the **ShopLite SKU** it supplies (e.g. `PT-00042`), and a discontinued flag. Discontinued items are flagged, never deleted. |
| `prices` | Current unit cost (USD) per catalog item |
| `stock` | Quantity the supplier has available per catalog item |

Every row has `updated_at`, set automatically by a trigger on every update.

**Schema `vendor_internal`:** `activity_runs`, one row per vendor day. ShopLite can't see it.

**Roles**

| Role | Can | Used by |
|---|---|---|
| database owner (admin) | everything, including structure | you, from your Mac: schema and seed |
| `shoplite_reader` | `SELECT` on the four `supply` tables, nothing else | ShopLite's ETL |
| `vendor_activity` | read and write vendor data and `activity_runs`; **no** `DELETE`, `TRUNCATE`, or structure changes | `activity.py`, `vendor-day.yml` |

## Passwords

Choose two new passwords in your password manager, one for `shoplite_reader` and one for
`vendor_activity`. Use letters, digits, and only `- _ . ~`: they go inside connection URLs, where
other symbols need escaping. Put them in your shell without them landing in your history:

```bash
read -rs SHOPLITE_READER_PASSWORD && export SHOPLITE_READER_PASSWORD     # type, press Enter
read -rs VENDOR_ACTIVITY_PASSWORD && export VENDOR_ACTIVITY_PASSWORD
```

Locally, put them in the **root `.env`** too (`SHOPLITE_READER_PASSWORD`, `VENDOR_ACTIVITY_PASSWORD`,
next to `POSTGRES_*`): every component builds its vendor URL from these parts, encoding special
characters (`tools/envconfig.py`), so you never write a local URL by hand. The `read -rs` lines above
are only for the `psql` step below, which reads them from your shell. In production the URLs come from
Neon (the GitHub secret, and the ETL's and collector's VM `.env` files).

---

## Local (Part C1)

From the repo root, with the root virtual environment active and Docker running:

```bash
pip install -r vendor/requirements.txt
```

**1. Schema and roles.** `psql` runs inside the `vendor-pg` container (nothing to install on the Mac);
it already knows the admin user and database from the container's settings:

```bash
docker exec -i -e RP="$SHOPLITE_READER_PASSWORD" -e AP="$VENDOR_ACTIVITY_PASSWORD" vendor-pg \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
         -v reader_password="$RP" -v activity_password="$AP"' \
  < vendor/sql/001_schema.sql
```

**2. Seed** (as the admin, using `POSTGRES_*` from the root `.env`):

```bash
python vendor/seed.py
```

It prints the number of suppliers, catalog items (about 10,000), and ShopLite products covered. To
start over: `python vendor/seed.py --reset`.

**3. One vendor day** (as `vendor_activity`):

```bash
python vendor/activity.py
```

It prints how many prices and stock levels changed and how many items were added and discontinued.
`--scale 3` makes a busier day.

**4. Check the read-only boundary:**

```bash
docker exec -i vendor-pg sh -c 'psql -U shoplite_reader -d "$POSTGRES_DB"' <<'SQL'
SELECT count(*) AS catalog_items FROM supply.catalog_items;        -- works
UPDATE supply.prices SET unit_cost = unit_cost WHERE false;       -- ERROR: permission denied
SELECT * FROM vendor_internal.activity_runs;                       -- ERROR: permission denied
SQL
```

The first query succeeds; the other two must fail with "permission denied". (Inside the container,
local connections don't ask for a password; the permissions are what's being tested.)

---

## Neon (Part C2)

**1. Create the project.** At https://neon.tech, create a project in a **US East** region (AWS
`us-east-1` or `us-east-2`) with Postgres 16, and note the database name (e.g. `vendordb`). From
**Connect**, copy the connection string of the **owner** role (`neondb_owner`), the **direct** (not
pooled) one, ending in `?sslmode=require`. That's the vendor admin URL: keep it in your password manager
and only in your shell:

```bash
read -rs VENDOR_ADMIN_URL && export VENDOR_ADMIN_URL
```

**2. Schema, roles, and seed** against Neon. `psql` runs in a throwaway `postgres:16` container:

```bash
docker run --rm -i -e VENDOR_ADMIN_URL -e RP="$SHOPLITE_READER_PASSWORD" -e AP="$VENDOR_ACTIVITY_PASSWORD" \
  postgres:16 sh -c 'psql "$VENDOR_ADMIN_URL" -v ON_ERROR_STOP=1 -v reader_password="$RP" -v activity_password="$AP"' \
  < vendor/sql/001_schema.sql

python vendor/seed.py            # uses VENDOR_ADMIN_URL
```

**3. The vendor-day secret.** Build the `vendor_activity` URL from the same host and database:

```
postgresql://vendor_activity:<VENDOR_ACTIVITY_PASSWORD>@<neon host>/<database>?sslmode=require
```

With a password of only letters, digits, and `- _ . ~` (as recommended above) it can go in as is.
Any other symbol must be percent-encoded in a URL (`@` → `%40`, `#` → `%23`, `%` → `%25`); this
prints the encoded form without the password landing in your history:

```bash
python3 -c "import getpass, urllib.parse; print(urllib.parse.quote(getpass.getpass(), safe=''))"
```

Add it in GitHub: **Settings → Secrets and variables → Actions → Secrets →** `VENDOR_ACTIVITY_URL`.
Then **Actions → Vendor day → Run workflow** once by hand, and read the summary (changes per kind).

**4. Confirm the reader can read but not write**, from your Mac:

```bash
READER_URL="postgresql://shoplite_reader:${SHOPLITE_READER_PASSWORD}@<neon host>/<database>?sslmode=require"
docker run --rm -i -e READER_URL postgres:16 sh -c 'psql "$READER_URL"' <<'SQL'
SELECT count(*) AS catalog_items FROM supply.catalog_items;        -- works
UPDATE supply.prices SET unit_cost = unit_cost WHERE false;       -- ERROR: permission denied
SELECT * FROM vendor_internal.activity_runs;                       -- ERROR: permission denied
SQL
```

**Neon's free tier sleeps.** Its compute suspends after a few minutes without queries, so the first
connection after a quiet period takes a few seconds longer. The scripts allow 15 seconds to connect.
For ShopLite, that's a realistic "the vendor is slow" signal, not a fault.

---

## The daily changes

`.github/workflows/vendor-day.yml` runs `activity.py` **every day at 12:00 UTC**, weekends included
(the vendor keeps working when ShopLite's VM is off), as `vendor_activity`. Each run, in one
transaction: about 8% of prices drift by up to ±3%, about 25% of stock levels move, about 20 items are
discontinued, and about 10 items are added. Each run is recorded in `vendor_internal.activity_runs`:

```sql
SELECT * FROM vendor_internal.activity_runs ORDER BY run_id DESC LIMIT 7;   -- as the admin
```

**Its own switch:** the repository variable `VENDOR_ACTIVITY_ENABLED`. The vendor is an external
system, so ShopLite's `TRAFFIC_ENABLED` doesn't pause it. A missing value counts as enabled; set it
to `false` to keep the vendor data still (for example while testing the ETL). If it stays off for
more than about a day and a half, the ETL will refuse the feed as stale (below).

## The data contract

`knowledge/contracts/vendor-supplier-feed.yml` says exactly what ShopLite expects from `supply.*`:
tables, columns and types, which may be empty, keys, value rules (SKU format, cost ≥ 0, USD), minimum
row counts, every catalog item having one price and one stock row, and **freshness: the newest price
change within 36 hours**. The ETL checks all of it before loading anything, and a difference stops
the load with a message naming it.

So the vendor may **add** columns, but renaming or removing a column, changing a type, or letting
prices go stale makes ShopLite's ETL refuse the feed. Changing the vendor schema on purpose means
changing the contract (and the ETL) in the same commit.
