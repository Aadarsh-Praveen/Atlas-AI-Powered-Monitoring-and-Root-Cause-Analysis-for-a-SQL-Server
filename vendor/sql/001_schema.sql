-- vendor/sql/001_schema.sql: the vendor's Postgres database (Phase 4).
--
-- "The vendor" is a supplier network outside Azure (Neon in production, the
-- vendor-pg container locally). ShopLite doesn't own this database: it gets a
-- READ-ONLY login limited to the tables the vendor shares, and reads them in
-- its nightly ETL (server/etl/).
--
-- Layout:
--   schema supply            the SHARED tables (ShopLite can read these)
--     suppliers              who supplies
--     catalog_items          each supplier's SKU, mapped to the ShopLite SKU it supplies
--     prices                 current unit cost per catalog item
--     stock                  current quantity the supplier has available
--   schema vendor_internal   the vendor's own bookkeeping (ShopLite can't see it)
--     activity_runs          one row per run of the daily activity script
--
-- Roles (LOGIN), passwords given at run time, never stored in a file:
--   shoplite_reader   SELECT on the four shared tables. Nothing else.
--   vendor_activity   what the daily activity script (vendor/activity.py,
--                     .github/workflows/vendor-day.yml) needs: read and write
--                     the vendor tables, no DELETE, no TRUNCATE, no DDL.
-- The database owner (the admin you connect as) owns everything and is the
-- only role that can change structure. Its connection stays on your Mac.
--
-- Safe to re-run: IF NOT EXISTS / OR REPLACE everywhere; no data is dropped.
-- Re-running also resets both role passwords to the values passed in.
--
-- Run (see vendor/README.md), with passwords from environment variables so
-- they don't land in your shell history:
--   psql "$VENDOR_ADMIN_URL" -v ON_ERROR_STOP=1 \
--        -v reader_password="$SHOPLITE_READER_PASSWORD" \
--        -v activity_password="$VENDOR_ACTIVITY_PASSWORD" \
--        -f vendor/sql/001_schema.sql

\set ON_ERROR_STOP on

\if :{?reader_password}
\else
  \echo 'Missing -v reader_password=... (password for shoplite_reader). Nothing was changed.'
  \quit
\endif
\if :{?activity_password}
\else
  \echo 'Missing -v activity_password=... (password for vendor_activity). Nothing was changed.'
  \quit
\endif

BEGIN;

-- ---------------------------------------------------------------------------
-- Schemas
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS supply;
CREATE SCHEMA IF NOT EXISTS vendor_internal;

-- ---------------------------------------------------------------------------
-- updated_at: set automatically on every UPDATE, so "freshness" can't be
-- forgotten by a script. ShopLite's data contract checks it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION supply.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- Shared tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supply.suppliers (
    supplier_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    supplier_code  varchar(10)  NOT NULL UNIQUE,      -- short code used in SKUs, e.g. NS
    name           varchar(100) NOT NULL UNIQUE,      -- e.g. Northline Supply Co.
    is_active      boolean      NOT NULL DEFAULT true,
    created_at     timestamptz  NOT NULL DEFAULT now(),
    updated_at     timestamptz  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS supply.catalog_items (
    catalog_item_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    supplier_id      integer      NOT NULL REFERENCES supply.suppliers (supplier_id),
    supplier_sku     varchar(40)  NOT NULL,           -- the supplier's own code
    shoplite_sku     varchar(20)  NOT NULL,           -- ShopLite's product SKU, e.g. PT-00042
    description      varchar(200) NOT NULL,
    -- Discontinued items stay (flagged), so ShopLite's ETL can see they're gone.
    is_discontinued  boolean      NOT NULL DEFAULT false,
    discontinued_at  timestamptz  NULL,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    updated_at       timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_catalog_items_supplier_sku UNIQUE (supplier_id, supplier_sku),
    CONSTRAINT ck_catalog_items_discontinued
        CHECK (is_discontinued = (discontinued_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_catalog_items_shoplite_sku ON supply.catalog_items (shoplite_sku);

CREATE TABLE IF NOT EXISTS supply.prices (
    catalog_item_id  integer       PRIMARY KEY REFERENCES supply.catalog_items (catalog_item_id),
    unit_cost        numeric(10,2) NOT NULL CHECK (unit_cost >= 0),   -- what ShopLite pays
    currency         char(3)       NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    updated_at       timestamptz   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS supply.stock (
    catalog_item_id     integer     PRIMARY KEY REFERENCES supply.catalog_items (catalog_item_id),
    quantity_available  integer     NOT NULL CHECK (quantity_available >= 0),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Triggers (dropped and recreated so re-running is safe).
DROP TRIGGER IF EXISTS trg_suppliers_touch ON supply.suppliers;
CREATE TRIGGER trg_suppliers_touch BEFORE UPDATE ON supply.suppliers
    FOR EACH ROW EXECUTE FUNCTION supply.touch_updated_at();
DROP TRIGGER IF EXISTS trg_catalog_items_touch ON supply.catalog_items;
CREATE TRIGGER trg_catalog_items_touch BEFORE UPDATE ON supply.catalog_items
    FOR EACH ROW EXECUTE FUNCTION supply.touch_updated_at();
DROP TRIGGER IF EXISTS trg_prices_touch ON supply.prices;
CREATE TRIGGER trg_prices_touch BEFORE UPDATE ON supply.prices
    FOR EACH ROW EXECUTE FUNCTION supply.touch_updated_at();
DROP TRIGGER IF EXISTS trg_stock_touch ON supply.stock;
CREATE TRIGGER trg_stock_touch BEFORE UPDATE ON supply.stock
    FOR EACH ROW EXECUTE FUNCTION supply.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Vendor-internal table (not shared)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vendor_internal.activity_runs (
    run_id             integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz NULL,
    prices_changed     integer     NOT NULL DEFAULT 0,
    stock_changed      integer     NOT NULL DEFAULT 0,
    items_added        integer     NOT NULL DEFAULT 0,
    items_discontinued integer     NOT NULL DEFAULT 0,
    note               varchar(200) NULL
);

COMMIT;

-- ---------------------------------------------------------------------------
-- Roles. CREATE ROLE can't take a psql variable inside a DO block, so each
-- role is created with \gexec only if it doesn't exist yet, then its password
-- is (re)set. format(%L) quotes the password safely.
-- ---------------------------------------------------------------------------
SELECT format('CREATE ROLE shoplite_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'shoplite_reader') \gexec
SELECT format('ALTER ROLE shoplite_reader LOGIN PASSWORD %L', :'reader_password') \gexec

SELECT format('CREATE ROLE vendor_activity LOGIN PASSWORD %L', :'activity_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vendor_activity') \gexec
SELECT format('ALTER ROLE vendor_activity LOGIN PASSWORD %L', :'activity_password') \gexec

BEGIN;

-- Start from nothing, then grant exactly what each role needs.
REVOKE ALL ON SCHEMA supply, vendor_internal FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA supply FROM shoplite_reader, vendor_activity;
REVOKE ALL ON ALL TABLES IN SCHEMA vendor_internal FROM shoplite_reader, vendor_activity;

-- ShopLite: read the shared tables, nothing else (no vendor_internal at all).
GRANT USAGE ON SCHEMA supply TO shoplite_reader;
GRANT SELECT ON supply.suppliers, supply.catalog_items, supply.prices, supply.stock TO shoplite_reader;

-- The daily activity script: read and change vendor data, record its runs.
-- No DELETE or TRUNCATE (discontinuing is a flag, not a delete), no DDL.
GRANT USAGE ON SCHEMA supply, vendor_internal TO vendor_activity;
GRANT SELECT ON supply.suppliers TO vendor_activity;
GRANT SELECT, INSERT, UPDATE ON supply.catalog_items, supply.prices, supply.stock TO vendor_activity;
GRANT SELECT, INSERT, UPDATE ON vendor_internal.activity_runs TO vendor_activity;

COMMIT;

\echo 'Vendor schema ready: supply.* shared with shoplite_reader (read-only); vendor_activity can update vendor data.'
