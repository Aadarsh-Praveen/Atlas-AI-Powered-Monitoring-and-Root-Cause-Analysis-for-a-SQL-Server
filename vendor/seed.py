"""Seed the vendor's Postgres database with suppliers and a catalog that maps
to ShopLite's products.

Run after vendor/sql/001_schema.sql, as the vendor database's ADMIN (owner):

    python vendor/seed.py                    # local vendor-pg (credentials from the root .env)
    VENDOR_ADMIN_URL="postgresql://...?sslmode=require" python vendor/seed.py   # Neon

How it maps to ShopLite without connecting to SalesDB: shoplite/db/seed.py
builds its products FIRST from random.Random(seed), and a product's SKU is
"<aisle prefix>-<product id>". Calling the same generator with the same seed
and product count reproduces ShopLite's exact SKUs and selling prices, the
same way locally and in production (both seeded with the defaults: seed 42,
5,000 products). If you ever seeded ShopLite with other --seed/--products
values, pass the same ones here.

What it creates:
  - 6 suppliers;
  - for each ShopLite product, 1-3 catalog items (one per supplier carrying it),
    with the supplier's own SKU, a unit cost of 45-75% of ShopLite's selling
    price, stock of 0-2,000 (about 5% at zero), and timestamps within the last
    day, so the data contract's freshness rule passes on day one.

Refuses to run if the vendor tables already have data, unless --reset, which
empties them first (TRUNCATE ... RESTART IDENTITY). The same --vendor-seed
always produces the same data.
"""

import argparse
import importlib.util
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from envconfig import load_layers, vendor_url  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SHOPLITE_SEED = REPO_ROOT / "shoplite" / "db" / "seed.py"

# The vendor's supplier network: (code, name). Same company names ShopLite's
# placeholder data used, so the cutover keeps familiar names.
SUPPLIERS = [
    ("NS", "Northline Supply Co."),
    ("ID", "Ironbridge Distributors"),
    ("KW", "Keystone Wholesale"),
    ("BP", "Bluewater Plumbing Supply"),
    ("SE", "Summit Electrical Supply"),
    ("GG", "Greenfield Garden Wholesale"),
]


def load_shoplite_products(seed: int, count: int) -> list[tuple[str, str, int]]:
    """ShopLite's products as (sku, name, price_cents), from its own generator.

    Loads shoplite/db/seed.py as a module (it only connects to SQL Server in
    its main(), which isn't called) and runs generate_products exactly as
    seed.py does: first, with a fresh random.Random(seed).
    """
    spec = importlib.util.spec_from_file_location("shoplite_seed", SHOPLITE_SEED)
    shoplite_seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shoplite_seed)

    rng = random.Random(seed)
    # The dates only affect created_at values, not SKUs, names, or prices.
    end = datetime.combine(date.today(), datetime.min.time())
    rows, price_cents, _created, _category = shoplite_seed.generate_products(
        rng, count, end - timedelta(days=730), end)
    # Product row layout (seed.py): product_id, sku, name, description, ...
    return [(row[1], row[2], price_cents[row[0]]) for row in rows]


def admin_url() -> str:
    """VENDOR_ADMIN_URL as given (Neon), else built from the layered settings
    (vendor/.env if present, then the root .env): POSTGRES_USER, POSTGRES_PASSWORD
    (percent-encoded), POSTGRES_DB, VENDOR_HOST (localhost), VENDOR_PORT (5433)."""
    load_layers(Path(__file__).resolve().parent)
    url = vendor_url("admin", explicit="VENDOR_ADMIN_URL")
    if not url:
        sys.exit("[FAIL] Set VENDOR_ADMIN_URL, or POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB in the root .env.")
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the vendor database.")
    parser.add_argument("--shoplite-seed", type=int, default=42, help="seed.py's --seed (default 42)")
    parser.add_argument("--products", type=int, default=5000, help="seed.py's --products (default 5000)")
    parser.add_argument("--vendor-seed", type=int, default=7, help="seed for the vendor's own choices")
    parser.add_argument("--reset", action="store_true", help="empty the vendor tables first")
    args = parser.parse_args()

    products = load_shoplite_products(args.shoplite_seed, args.products)
    rng = random.Random(args.vendor_seed)
    now = datetime.now(timezone.utc)

    # connect_timeout: an unreachable or sleeping database fails within 15 s
    # (Neon's free tier can take a few seconds to wake up).
    with psycopg.connect(admin_url(), connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM supply.catalog_items")
            existing = cur.fetchone()[0]
            if existing and not args.reset:
                print(f"[FAIL] The vendor catalog already has {existing:,} items. Use --reset to start over.")
                return 1
            if args.reset:
                cur.execute("TRUNCATE supply.stock, supply.prices, supply.catalog_items, supply.suppliers,"
                            " vendor_internal.activity_runs RESTART IDENTITY")

            # Suppliers
            supplier_ids = {}
            for code, name in SUPPLIERS:
                cur.execute("INSERT INTO supply.suppliers (supplier_code, name) VALUES (%s, %s)"
                            " RETURNING supplier_id", (code, name))
                supplier_ids[code] = cur.fetchone()[0]

            # Catalog items, then their prices and stock. COPY streams rows in
            # bulk: far faster than one INSERT per row for ~10,000 items.
            items = []          # (supplier_id, supplier_sku, shoplite_sku, description, cost, stock, stamp)
            for sku, name, price_cents in products:
                for code, _name in rng.sample(SUPPLIERS, rng.randint(1, 3)):
                    cost = round(price_cents * rng.uniform(0.45, 0.75)) / 100
                    stock = 0 if rng.random() < 0.05 else rng.randint(1, 2000)
                    stamp = now - timedelta(hours=rng.uniform(0.5, 23.5))
                    supplier_sku = f"{code}-{rng.randint(100000, 999999)}-{sku[-5:]}"
                    items.append((supplier_ids[code], supplier_sku, sku, name[:200], cost, stock, stamp))

            with cur.copy("COPY supply.catalog_items (supplier_id, supplier_sku, shoplite_sku, description,"
                          " created_at, updated_at) FROM STDIN") as copy:
                for supplier_id, supplier_sku, sku, description, _cost, _stock, stamp in items:
                    copy.write_row((supplier_id, supplier_sku, sku, description, stamp, stamp))

            # Look up each new item's id by its natural key (supplier, supplier
            # SKU) rather than assuming ids 1..N, which only holds right after
            # a RESTART IDENTITY.
            cur.execute("SELECT supplier_id, supplier_sku, catalog_item_id FROM supply.catalog_items")
            item_ids = {(supplier_id, supplier_sku): item_id for supplier_id, supplier_sku, item_id in cur}

            with cur.copy("COPY supply.prices (catalog_item_id, unit_cost, updated_at) FROM STDIN") as copy:
                for supplier_id, supplier_sku, _sku, _d, cost, _st, stamp in items:
                    copy.write_row((item_ids[(supplier_id, supplier_sku)], cost, stamp))
            with cur.copy("COPY supply.stock (catalog_item_id, quantity_available, updated_at) FROM STDIN") as copy:
                for supplier_id, supplier_sku, _sku, _d, _c, stock, stamp in items:
                    copy.write_row((item_ids[(supplier_id, supplier_sku)], stock, stamp))

            cur.execute("SELECT count(*), count(DISTINCT shoplite_sku) FROM supply.catalog_items")
            item_count, sku_count = cur.fetchone()
        conn.commit()

    print(f"[OK] Vendor seeded: {len(SUPPLIERS)} suppliers, {item_count:,} catalog items "
          f"covering {sku_count:,} ShopLite products (ShopLite seed {args.shoplite_seed}, "
          f"{args.products:,} products; vendor seed {args.vendor_seed}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
