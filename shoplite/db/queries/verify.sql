/*
    queries/verify.sql
    Sanity checks for SalesDB after seed.py. Read-only; run as often as you like.

    How to run: in VS Code's SQL Server extension, connect to localhost,1433,
    open this file, and run it (or select one query and run just that).
    Each query returns its own result grid.

    Part 1 (realism):   read the results and judge whether they look real.
    Part 2 (integrity): every row should say PASS.

    All timestamps are UTC. This file has no number prefix, so
    apply_schema.py never runs it.
*/

USE SalesDB;
GO

-- ===========================================================================
-- Part 1: Realism
-- ===========================================================================

-- 1.1 Row counts. Expect the seed defaults (5,000 / 50,000 / 200,000) and
--     about 2-2.5 order items per order. carts and cart_items stay empty
--     until the API is used.
SELECT 'dbo.categories' AS table_name, COUNT(*) AS row_count FROM dbo.categories
UNION ALL SELECT 'dbo.products',           COUNT(*) FROM dbo.products
UNION ALL SELECT 'dbo.inventory',          COUNT(*) FROM dbo.inventory
UNION ALL SELECT 'dbo.customers',          COUNT(*) FROM dbo.customers
UNION ALL SELECT 'dbo.carts',              COUNT(*) FROM dbo.carts
UNION ALL SELECT 'dbo.cart_items',         COUNT(*) FROM dbo.cart_items
UNION ALL SELECT 'dbo.orders',             COUNT(*) FROM dbo.orders
UNION ALL SELECT 'dbo.order_items',        COUNT(*) FROM dbo.order_items
UNION ALL SELECT 'dbo.payments',           COUNT(*) FROM dbo.payments
UNION ALL SELECT 'vendor.supplier_prices', COUNT(*) FROM vendor.supplier_prices;
GO

-- 1.2 Best sellers. Expect a long tail: the top products each hold a few
--     percent of all units, far more than the average product (0.02% with
--     5,000 products).
SELECT TOP (10)
    p.product_id,
    p.sku,
    p.name,
    SUM(oi.quantity)                                                  AS units_sold,
    CAST(100.0 * SUM(oi.quantity) / SUM(SUM(oi.quantity)) OVER () AS DECIMAL(5,2))
                                                                      AS pct_of_all_units
FROM dbo.order_items AS oi
JOIN dbo.products AS p ON p.product_id = oi.product_id
GROUP BY p.product_id, p.sku, p.name
ORDER BY units_sold DESC;
GO

-- 1.3 Orders per month. Expect spring (Apr-May) and holiday (Nov-Dec) peaks,
--     quiet Jan-Feb, and the same month a year later slightly higher (growth).
--     The first and last months may be partial.
SELECT
    YEAR(created_at)    AS order_year,
    MONTH(created_at)   AS order_month,
    COUNT(*)            AS orders,
    SUM(total)          AS revenue
FROM dbo.orders
GROUP BY YEAR(created_at), MONTH(created_at)
ORDER BY order_year, order_month;
GO

-- 1.4 Orders by weekday (UTC). Expect Saturday and Sunday highest.
--     DATENAME depends on the session language; English by default.
SELECT
    DATENAME(WEEKDAY, created_at)  AS weekday,
    COUNT(*)                       AS orders
FROM dbo.orders
GROUP BY DATENAME(WEEKDAY, created_at), DATEPART(WEEKDAY, created_at)
ORDER BY DATEPART(WEEKDAY, created_at);
GO

-- 1.5 Busiest customers. Expect a few customers with dozens of orders while
--     the typical customer has only a handful.
SELECT TOP (10)
    c.customer_id,
    c.email,
    c.state,
    COUNT(*)        AS orders,
    SUM(o.total)    AS lifetime_spend
FROM dbo.orders AS o
JOIN dbo.customers AS c ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.email, c.state
ORDER BY orders DESC;
GO

-- 1.6 Customers by number of orders. Expect most customers in the low
--     buckets and a thin tail of heavy buyers.
WITH per_customer AS (
    SELECT c.customer_id, COUNT(o.order_id) AS orders
    FROM dbo.customers AS c
    LEFT JOIN dbo.orders AS o ON o.customer_id = c.customer_id
    GROUP BY c.customer_id
)
SELECT
    CASE
        WHEN orders = 0  THEN '0'
        WHEN orders = 1  THEN '1'
        WHEN orders <= 3 THEN '2-3'
        WHEN orders <= 9 THEN '4-9'
        WHEN orders <= 24 THEN '10-24'
        ELSE '25+'
    END                 AS orders_per_customer,
    COUNT(*)            AS customers
FROM per_customer
GROUP BY
    CASE
        WHEN orders = 0  THEN '0'
        WHEN orders = 1  THEN '1'
        WHEN orders <= 3 THEN '2-3'
        WHEN orders <= 9 THEN '4-9'
        WHEN orders <= 24 THEN '10-24'
        ELSE '25+'
    END
ORDER BY MIN(orders);
GO

-- 1.7 Stock levels. Expect roughly 3% out of stock (for testing the 409)
--     and roughly 8% at or below the reorder level (the admin low-stock filter).
SELECT
    CASE
        WHEN quantity_on_hand = 0               THEN 'out of stock'
        WHEN quantity_on_hand <= reorder_level  THEN 'low stock'
        ELSE 'in stock'
    END         AS stock_status,
    COUNT(*)    AS products
FROM dbo.inventory
GROUP BY
    CASE
        WHEN quantity_on_hand = 0               THEN 'out of stock'
        WHEN quantity_on_hand <= reorder_level  THEN 'low stock'
        ELSE 'in stock'
    END;
GO

-- 1.8 Database settings from 001_create_database.sql.
--     Expect recovery_model SIMPLE and Query Store READ_WRITE.
SELECT
    d.name                          AS database_name,
    d.recovery_model_desc           AS recovery_model,
    d.is_read_committed_snapshot_on AS rcsi_on,          -- expect 0 (deliberately off)
    q.actual_state_desc             AS query_store_state,
    q.interval_length_minutes       AS query_store_interval_minutes
FROM sys.databases AS d
CROSS JOIN sys.database_query_store_options AS q
WHERE d.name = DB_NAME();
GO

-- ===========================================================================
-- Part 2: Integrity (every row should say PASS)
-- ===========================================================================

/*
    2.1 Identity counters continue after the highest IDs.
    New rows from the API get IDENT_CURRENT + 1, so the counter must be at
    least MAX(id), or the next insert would collide with a seeded row.
    ">=" rather than "=": gaps are normal. A rolled-back insert still uses up
    an identity value, so the counter can be ahead of MAX(id). That's fine;
    only behind is a problem.
    For a table that has never had a row (dbo.carts right after seeding),
    IDENT_CURRENT shows the seed value (1) and next_id reads 2, but the first
    row actually gets 1. That row is marked "PASS (empty)".
*/
SELECT
    t.table_name,
    t.max_id,
    IDENT_CURRENT(t.table_name)       AS identity_current,
    IDENT_CURRENT(t.table_name) + 1   AS next_id,
    CASE
        WHEN t.max_id IS NULL                          THEN 'PASS (empty)'
        WHEN IDENT_CURRENT(t.table_name) >= t.max_id   THEN 'PASS'
        ELSE 'FAIL'
    END                               AS result
FROM (VALUES
    ('dbo.categories',         (SELECT MAX(category_id)       FROM dbo.categories)),
    ('dbo.products',           (SELECT MAX(product_id)        FROM dbo.products)),
    ('dbo.customers',          (SELECT MAX(customer_id)       FROM dbo.customers)),
    ('dbo.carts',              (SELECT MAX(cart_id)           FROM dbo.carts)),
    ('dbo.orders',             (SELECT MAX(order_id)          FROM dbo.orders)),
    ('dbo.order_items',        (SELECT MAX(order_item_id)     FROM dbo.order_items)),
    ('dbo.payments',           (SELECT MAX(payment_id)        FROM dbo.payments)),
    ('vendor.supplier_prices', (SELECT MAX(supplier_price_id) FROM vendor.supplier_prices))
) AS t (table_name, max_id);
GO

/*
    2.2 Payments, totals, and timeline.
    Each row is one rule and the number of rows breaking it.

    Orders still in pending_payment are excluded from the payment rules:
    during checkout, the API creates the order first and records the payment
    a moment later, so a pending order without a payment is normal.
*/
WITH order_payments AS (
    SELECT
        o.order_id,
        o.status,
        o.total,
        o.created_at,
        COUNT(p.payment_id)  AS payment_count,
        MIN(p.amount)        AS payment_amount,     -- with exactly one payment,
        MIN(p.status)        AS payment_status,     -- MIN is simply that payment's value
        MIN(p.created_at)    AS payment_at
    FROM dbo.orders AS o
    LEFT JOIN dbo.payments AS p ON p.order_id = o.order_id
    WHERE o.status <> 'pending_payment'
    GROUP BY o.order_id, o.status, o.total, o.created_at
),
order_amounts AS (
    SELECT
        o.order_id,
        o.subtotal,
        o.shipping_cost,
        o.total,
        COUNT(oi.order_item_id)               AS item_count,
        SUM(oi.quantity * oi.unit_price)      AS items_subtotal
    FROM dbo.orders AS o
    LEFT JOIN dbo.order_items AS oi ON oi.order_id = o.order_id
    GROUP BY o.order_id, o.subtotal, o.shipping_cost, o.total
)
SELECT
    check_name,
    problems,
    CASE WHEN problems = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM (
    -- Payments
    SELECT 'Every order has exactly one payment' AS check_name,
           COUNT(*) AS problems
    FROM order_payments WHERE payment_count <> 1

    UNION ALL
    SELECT 'Payment amount equals order total',
           COUNT(*)
    FROM order_payments WHERE payment_count = 1 AND payment_amount <> total

    UNION ALL
    SELECT 'Declined payment if and only if order failed or cancelled',
           COUNT(*)
    FROM order_payments
    WHERE payment_count = 1
      AND (   (status IN ('payment_failed', 'cancelled')     AND payment_status <> 'declined')
           OR (status NOT IN ('payment_failed', 'cancelled') AND payment_status <> 'approved'))

    UNION ALL
    SELECT 'Payment recorded at or after the order',
           COUNT(*)
    FROM order_payments WHERE payment_count = 1 AND payment_at < created_at

    -- Totals
    UNION ALL
    SELECT 'Every order has at least one item',
           COUNT(*)
    FROM order_amounts WHERE item_count = 0

    UNION ALL
    SELECT 'Subtotal equals sum of quantity * unit_price',
           COUNT(*)
    FROM order_amounts WHERE item_count > 0 AND subtotal <> items_subtotal

    UNION ALL
    SELECT 'Total equals subtotal + shipping',
           COUNT(*)
    FROM order_amounts WHERE total <> subtotal + shipping_cost

    -- Timeline
    UNION ALL
    SELECT 'No order before its customer signed up',
           COUNT(*)
    FROM dbo.orders AS o
    JOIN dbo.customers AS c ON c.customer_id = o.customer_id
    WHERE o.created_at < c.created_at

    UNION ALL
    SELECT 'No product sold before it was added',
           COUNT(*)
    FROM dbo.order_items AS oi
    JOIN dbo.orders AS o   ON o.order_id = oi.order_id
    JOIN dbo.products AS p ON p.product_id = oi.product_id
    WHERE o.created_at < p.created_at

    UNION ALL
    SELECT 'No orders in the future',
           COUNT(*)
    FROM dbo.orders WHERE created_at > SYSUTCDATETIME()

    UNION ALL
    SELECT 'Order updated_at is not before created_at',
           COUNT(*)
    FROM dbo.orders WHERE updated_at < created_at

    -- Catalog
    UNION ALL
    -- problems = how many names are used by more than one active product.
    SELECT 'No two active products share a name',
           COUNT(*)
    FROM (
        SELECT name
        FROM dbo.products
        WHERE is_active = 1
        GROUP BY name
        HAVING COUNT(*) > 1
    ) AS duplicate_names
) AS checks (check_name, problems);
GO
