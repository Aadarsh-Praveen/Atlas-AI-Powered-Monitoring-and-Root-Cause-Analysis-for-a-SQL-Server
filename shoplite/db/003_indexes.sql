/*
    003_indexes.sql
    Secondary indexes for the queries the ShopLite API actually runs.

    Each index has a comment naming the endpoint and query it serves.
    Phase 5 deliberately drops some of these as incident scenarios; the
    comment is how a slowdown gets traced back to the missing index.

    Naming: IX_<table>_<key columns> for ordinary indexes,
            UX_<table>_<purpose> for unique ones.

    Reminder: SQL Server indexes PRIMARY KEY and UNIQUE constraints
    automatically (see 002_tables.sql) but NOT foreign key columns.
    Every FK column the API filters or joins on needs an index here.
*/

USE SalesDB;
GO

-- Filtered indexes (UX_carts_customer_open below) require these settings.
-- SET QUOTED_IDENTIFIER takes effect when a batch is parsed, so it gets its
-- own batch before the CREATE INDEX statements. See the note on that index.
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- ===========================================================================
-- Orders
-- ===========================================================================

-- GET /api/orders: a customer's order history, newest first.
--   WHERE customer_id = @customer_id ORDER BY created_at DESC
-- Key order matters: customer_id first finds the customer's rows, then
-- created_at DESC returns them already sorted, so no sort step is needed.
-- INCLUDE carries the columns the list shows, so SQL Server doesn't have to
-- look each order up in the table (a "key lookup").
CREATE NONCLUSTERED INDEX IX_orders_customer_id_created_at
    ON dbo.orders (customer_id, created_at DESC)
    INCLUDE (status, total);
GO

-- GET /api/admin/reports/sales and /top-products (via 005 procedures):
--   WHERE created_at >= @start AND created_at < @end
-- Lets the reports read just the date range instead of every order.
-- The reports are still heavy (they aggregate many rows); that's intended.
CREATE NONCLUSTERED INDEX IX_orders_created_at
    ON dbo.orders (created_at)
    INCLUDE (status, total, ship_state);
GO

-- ===========================================================================
-- Order items
-- ===========================================================================

-- GET /api/orders/{id}, POST /api/orders/{id}/reorder:
--   the items of one order. WHERE order_id = @order_id
-- Also used by the reports when joining orders to their items.
CREATE NONCLUSTERED INDEX IX_order_items_order_id
    ON dbo.order_items (order_id)
    INCLUDE (product_id, quantity, unit_price);
GO

-- GET /api/products/{id} "often bought together":
--   find orders containing this product (WHERE product_id = @product_id),
--   then the other products in those orders (via IX_order_items_order_id).
-- Also serves the top-products report, which groups by product_id.
CREATE NONCLUSTERED INDEX IX_order_items_product_id
    ON dbo.order_items (product_id)
    INCLUDE (order_id, quantity, unit_price);
GO

-- ===========================================================================
-- Payments
-- ===========================================================================

-- GET /api/orders/{id} (show payment) and POST /api/checkout (second
-- transaction records the payment for the order). WHERE order_id = @order_id
CREATE NONCLUSTERED INDEX IX_payments_order_id
    ON dbo.payments (order_id)
    INCLUDE (status, method, amount);
GO

-- ===========================================================================
-- Products
-- ===========================================================================

-- GET /api/products?category=...: an aisle's products, sorted by name,
-- price, or newest. WHERE category_id = @category_id AND is_active = 1
-- INCLUDE covers the columns a product tile shows, so browsing an aisle
-- reads only this index.
CREATE NONCLUSTERED INDEX IX_products_category_id
    ON dbo.products (category_id, is_active)
    INCLUDE (sku, name, price, created_at);
GO

-- ===========================================================================
-- Carts
-- ===========================================================================

/*
    GET /api/cart and POST /api/cart/items: find the customer's open cart.
        WHERE customer_id = @customer_id AND status = 'open'

    This is a FILTERED UNIQUE index: it only contains rows where
    status = 'open', and it's unique on customer_id. So it does two jobs:
      1. Finds a customer's open cart with a single seek.
      2. Guarantees at most ONE open cart per customer. If two requests race
         to create a cart, the second INSERT fails instead of silently
         creating a duplicate. Converted and abandoned carts aren't in the
         index, so a customer can have any number of those.

    SET options requirement (important, and easy to trip over):
      Filtered indexes (like indexed views and indexes on computed columns)
      require ANSI_NULLS ON and QUOTED_IDENTIFIER ON (plus ANSI_PADDING,
      ANSI_WARNINGS, ARITHABORT, CONCAT_NULL_YIELDS_NULL ON and
      NUMERIC_ROUNDABORT OFF, which are already the defaults for modern
      drivers). And not just when the index is CREATED: every INSERT,
      UPDATE, DELETE, or MERGE on dbo.carts, and every index rebuild, must
      also run with these settings, from ANY connection. Otherwise it fails
      with error 1934:
        "INSERT failed because the following SET options have incorrect
         settings: 'QUOTED_IDENTIFIER'."

      - pyodbc / ODBC Driver 18 (apply_schema.py, the API) turn these ON
        by default, so they're fine.
      - The sqlcmd gotcha: the classic sqlcmd utility connects with
        QUOTED_IDENTIFIER OFF unless you pass -I. Running this script with
        plain `sqlcmd -i 003_indexes.sql` would fail on this index (the
        SET at the top of this file avoids that for this script, but not
        for other scripts that later write to dbo.carts). Always pass -I
        to sqlcmd in this project.
      - Stored procedures remember the QUOTED_IDENTIFIER and ANSI_NULLS
        values in effect when they were CREATED, not when they run. A
        procedure created with them OFF will fail with 1934 when it writes
        to dbo.carts, no matter how the caller is connected.
      - Matters later: SQL Agent job steps and maintenance scripts (Phase 4)
        are a classic place for this error to appear, for example an index
        rebuild job failing on this table. If Atlas ever sees error 1934,
        this comment is the explanation.
*/
CREATE UNIQUE NONCLUSTERED INDEX UX_carts_customer_open
    ON dbo.carts (customer_id)
    WHERE status = 'open';
GO

/*
    Deliberately NOT indexed:

    - Product search (name LIKE '%drill%'): a leading wildcard can't use a
      normal B-tree index, so search scans the products table. With 5,000
      products that's acceptable, and it's a realistic, visible cost.
      Full-text search would be the fix if it ever mattered.
    - Low-stock filter (quantity_on_hand <= reorder_level): compares two
      columns, which an index can't seek on. inventory has one row per
      product (5,000 rows), so a scan is cheap.
    - cart_items: its primary key (cart_id, product_id) already covers
      "the items in this cart".
    - Foreign keys the API never filters on (orders.cart_id,
      cart_items.product_id, vendor.supplier_prices.product_id) stay
      unindexed for now. The Phase 4 ETL may need one on supplier_prices.
*/
