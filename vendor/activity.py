"""One "vendor day": the vendor's prices, stock, and catalog change a little.

Real suppliers don't sit still. Every day, weekends included
(.github/workflows/vendor-day.yml, 12:00 UTC, before ShopLite's weekday 14:00
ETL), this script:

  - drifts about 8% of prices by up to +/-3% (never below $0.10);
  - moves about 25% of stock levels: mostly selling down, sometimes restocked;
  - discontinues about 20 catalog items (flagged with a time, stock set to 0;
    rows are never deleted, so ShopLite's ETL sees them go);
  - adds about 10 catalog items: a supplier starts carrying a ShopLite product
    it didn't carry before, priced near what the others charge.

Everything happens in ONE transaction (all of the day's changes or none), and
the run is recorded in vendor_internal.activity_runs. Touched rows get a new
updated_at from the table triggers, which keeps the feed "fresh" for
ShopLite's data contract (newest price change within 36 hours).

Runs as the vendor_activity role (read and write vendor data, no DELETE, no
DDL), never as the database admin:

    python vendor/activity.py                       # local: role password from VENDOR_ACTIVITY_PASSWORD
    VENDOR_ACTIVITY_URL="postgresql://vendor_activity:...@...neon.tech/...?sslmode=require" python vendor/activity.py

Options: --scale 2 doubles every change (a busy day); --seed N repeats a run.
"""

import argparse
import os
import random
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from envconfig import load_layers, vendor_url  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def activity_url() -> str:
    """VENDOR_ACTIVITY_URL as given (Neon, GitHub Actions: no .env files there,
    so nothing else is read), else built from the layered settings (vendor/.env
    if present, then the root .env): VENDOR_ACTIVITY_PASSWORD (percent-encoded),
    POSTGRES_DB, VENDOR_HOST (localhost), VENDOR_PORT (5433)."""
    load_layers(Path(__file__).resolve().parent)
    url = vendor_url("activity", explicit="VENDOR_ACTIVITY_URL")
    if not url:
        sys.exit("[FAIL] Set VENDOR_ACTIVITY_URL, or VENDOR_ACTIVITY_PASSWORD and POSTGRES_DB in the root .env.")
    return url


def write_summary(lines: list[str]) -> None:
    text = "\n".join(lines)
    print(text)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate one day of vendor changes.")
    parser.add_argument("--scale", type=float, default=1.0, help="multiply every change (default 1)")
    parser.add_argument("--seed", type=int, help="repeat a run's choices (default: random)")
    args = parser.parse_args()
    if not 0 < args.scale <= 10:
        sys.exit("[FAIL] --scale must be between 0 and 10.")

    rng = random.Random(args.seed)
    price_share = min(0.08 * args.scale, 1.0)
    stock_share = min(0.25 * args.scale, 1.0)
    to_discontinue = round(20 * args.scale)
    to_add = round(10 * args.scale)

    # connect_timeout: fail fast if the vendor is unreachable (Neon's free tier
    # may take a few seconds to wake). statement_timeout: no single statement
    # may run longer than 60 s.
    with psycopg.connect(activity_url(), connect_timeout=15,
                         options="-c statement_timeout=60000") as conn:
        with conn.cursor() as cur:
            # Postgres's random() is seeded from Python's choice, so --seed
            # repeats the database-side choices too.
            cur.execute("SELECT setseed(%s)", (rng.uniform(-1, 1),))
            cur.execute("INSERT INTO vendor_internal.activity_runs (note) VALUES (%s) RETURNING run_id",
                        (f"scale {args.scale}",))
            run_id = cur.fetchone()[0]

            # 1. Price drift on active items: up to +/-3%, rounded to cents.
            #    random() returns double precision, so numeric * random() is
            #    double precision too, and Postgres's round(value, places)
            #    only exists for numeric: cast back to numeric before rounding.
            cur.execute("""
                UPDATE supply.prices AS p
                SET unit_cost = GREATEST(0.10, round((p.unit_cost * (1 + (random() - 0.5) * 0.06))::numeric, 2))
                FROM supply.catalog_items AS c
                WHERE c.catalog_item_id = p.catalog_item_id
                  AND NOT c.is_discontinued
                  AND random() < %s
            """, (price_share,))
            prices_changed = cur.rowcount

            # 2. Stock moves: 65% sell down by 1-150, 35% restock by 100-800.
            cur.execute("""
                UPDATE supply.stock AS s
                SET quantity_available = GREATEST(0, s.quantity_available +
                    CASE WHEN random() < 0.65 THEN -(1 + floor(random() * 150))::int
                         ELSE (100 + floor(random() * 700))::int END)
                FROM supply.catalog_items AS c
                WHERE c.catalog_item_id = s.catalog_item_id
                  AND NOT c.is_discontinued
                  AND random() < %s
            """, (stock_share,))
            stock_changed = cur.rowcount

            # 3. Discontinue a few items (flag + time; their stock goes to 0).
            cur.execute("""
                WITH chosen AS (
                    SELECT catalog_item_id FROM supply.catalog_items
                    WHERE NOT is_discontinued ORDER BY random() LIMIT %s
                )
                UPDATE supply.catalog_items AS c
                SET is_discontinued = true, discontinued_at = now()
                FROM chosen WHERE c.catalog_item_id = chosen.catalog_item_id
                RETURNING c.catalog_item_id
            """, (to_discontinue,))
            discontinued_ids = [row[0] for row in cur.fetchall()]
            if discontinued_ids:
                cur.execute("UPDATE supply.stock SET quantity_available = 0 WHERE catalog_item_id = ANY(%s)",
                            (discontinued_ids,))

            # 4. New items: a supplier starts carrying a product it didn't carry.
            cur.execute("SELECT supplier_id, supplier_code FROM supply.suppliers WHERE is_active")
            suppliers = cur.fetchall()
            cur.execute("""
                SELECT c.shoplite_sku, min(c.description), avg(p.unit_cost),
                       array_agg(c.supplier_id)
                FROM supply.catalog_items AS c
                JOIN supply.prices AS p ON p.catalog_item_id = c.catalog_item_id
                WHERE NOT c.is_discontinued
                GROUP BY c.shoplite_sku
                ORDER BY random()
                LIMIT %s
            """, (to_add * 3,))
            candidates = cur.fetchall()

            added = 0
            for sku, description, avg_cost, carried_by in candidates:
                if added >= to_add:
                    break
                free = [(sid, code) for sid, code in suppliers if sid not in carried_by]
                if not free:
                    continue
                supplier_id, code = rng.choice(free)
                supplier_sku = f"{code}-{rng.randint(100000, 999999)}-{sku[-5:]}"
                cur.execute("""
                    INSERT INTO supply.catalog_items (supplier_id, supplier_sku, shoplite_sku, description)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (supplier_id, supplier_sku) DO NOTHING
                    RETURNING catalog_item_id
                """, (supplier_id, supplier_sku, sku, description))
                row = cur.fetchone()
                if row is None:                          # SKU collision: skip, very rare
                    continue
                cost = max(0.10, round(float(avg_cost) * rng.uniform(0.9, 1.1), 2))
                cur.execute("INSERT INTO supply.prices (catalog_item_id, unit_cost) VALUES (%s, %s)",
                            (row[0], cost))
                cur.execute("INSERT INTO supply.stock (catalog_item_id, quantity_available) VALUES (%s, %s)",
                            (row[0], rng.randint(50, 1500)))
                added += 1

            cur.execute("""
                UPDATE vendor_internal.activity_runs
                SET finished_at = now(), prices_changed = %s, stock_changed = %s,
                    items_added = %s, items_discontinued = %s
                WHERE run_id = %s
            """, (prices_changed, stock_changed, added, len(discontinued_ids), run_id))
        conn.commit()                                   # the whole day, or nothing

    write_summary([
        "## Vendor day",
        "",
        "| Prices changed | Stock changed | Items added | Items discontinued |",
        "|---|---|---|---|",
        f"| {prices_changed:,} | {stock_changed:,} | {added} | {len(discontinued_ids)} |",
        "",
        f"Recorded as vendor_internal.activity_runs #{run_id}.",
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
