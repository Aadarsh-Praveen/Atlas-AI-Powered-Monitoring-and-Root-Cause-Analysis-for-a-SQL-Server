/*
    002_tables.sql
    Creates the SalesDB tables: nine in dbo, one in the vendor schema.

    Conventions used throughout:
      - Every table has a named primary key (PK_<table>).
      - Relationships use named foreign keys (FK_<child>_<parent>).
      - Allowed values are enforced with CHECK constraints (CK_...).
      - Money is DECIMAL, never FLOAT: binary floating point can't store
        values like 0.10 exactly, and rounding errors add up in totals.
      - Timestamps are DATETIME2(3) in UTC, defaulting to SYSUTCDATETIME().
        The storefront converts to local time for display.
      - Names and free text are NVARCHAR (Unicode); codes are VARCHAR.
      - Secondary indexes live in 003_indexes.sql, not here. Only the indexes
        created automatically by PRIMARY KEY and UNIQUE constraints exist
        after this script.

    Tables are created parents first, so each foreign key's target exists.
*/

USE SalesDB;
GO

-- ===========================================================================
-- Catalog
-- ===========================================================================

-- Store aisles: hand tools, power tools, fasteners, and so on.
CREATE TABLE dbo.categories
(
    category_id   INT IDENTITY(1,1) NOT NULL,
    name          NVARCHAR(100)     NOT NULL,
    slug          VARCHAR(100)      NOT NULL,   -- URL-friendly name, e.g. 'power-tools'
    description   NVARCHAR(400)     NULL,
    sort_order    INT               NOT NULL CONSTRAINT DF_categories_sort_order DEFAULT (0),

    CONSTRAINT PK_categories PRIMARY KEY (category_id),
    CONSTRAINT UQ_categories_name UNIQUE (name),
    CONSTRAINT UQ_categories_slug UNIQUE (slug)
);
GO

CREATE TABLE dbo.products
(
    product_id    INT IDENTITY(1,1) NOT NULL,
    sku           VARCHAR(20)       NOT NULL,   -- shown on the yellow shelf tag
    name          NVARCHAR(200)     NOT NULL,
    description   NVARCHAR(1000)    NULL,
    category_id   INT               NOT NULL,
    brand         NVARCHAR(100)     NULL,
    price         DECIMAL(10,2)     NOT NULL,   -- current selling price
    is_active     BIT               NOT NULL CONSTRAINT DF_products_is_active DEFAULT (1),
    created_at    DATETIME2(3)      NOT NULL CONSTRAINT DF_products_created_at DEFAULT (SYSUTCDATETIME()),

    CONSTRAINT PK_products PRIMARY KEY (product_id),
    CONSTRAINT UQ_products_sku UNIQUE (sku),
    CONSTRAINT FK_products_categories FOREIGN KEY (category_id) REFERENCES dbo.categories (category_id),
    CONSTRAINT CK_products_price CHECK (price >= 0)
);
GO

-- One row per product: a single store, no warehouses.
-- Checkout locks these rows (UPDLOCK) while it checks and reduces stock.
CREATE TABLE dbo.inventory
(
    product_id        INT          NOT NULL,
    quantity_on_hand  INT          NOT NULL,
    reorder_level     INT          NOT NULL CONSTRAINT DF_inventory_reorder_level DEFAULT (10),  -- "low stock" threshold for the admin filter
    updated_at        DATETIME2(3) NOT NULL CONSTRAINT DF_inventory_updated_at DEFAULT (SYSUTCDATETIME()),

    CONSTRAINT PK_inventory PRIMARY KEY (product_id),
    CONSTRAINT FK_inventory_products FOREIGN KEY (product_id) REFERENCES dbo.products (product_id),
    -- Last line of defence: even if application code has a bug, stock can't go negative.
    CONSTRAINT CK_inventory_quantity CHECK (quantity_on_hand >= 0),
    CONSTRAINT CK_inventory_reorder_level CHECK (reorder_level >= 0)
);
GO

-- ===========================================================================
-- Customers and carts
-- ===========================================================================

CREATE TABLE dbo.customers
(
    customer_id    INT IDENTITY(1,1) NOT NULL,
    email          NVARCHAR(256)     NOT NULL,   -- demo sign-in looks customers up by email
    first_name     NVARCHAR(100)     NOT NULL,
    last_name      NVARCHAR(100)     NOT NULL,
    phone          VARCHAR(30)       NULL,
    address_line1  NVARCHAR(200)     NOT NULL,
    city           NVARCHAR(100)     NOT NULL,
    state          CHAR(2)           NOT NULL,   -- US state code; the sales report groups by it
    postal_code    VARCHAR(10)       NOT NULL,
    created_at     DATETIME2(3)      NOT NULL CONSTRAINT DF_customers_created_at DEFAULT (SYSUTCDATETIME()),

    CONSTRAINT PK_customers PRIMARY KEY (customer_id),
    -- The unique constraint also creates the index that sign-in uses.
    CONSTRAINT UQ_customers_email UNIQUE (email)
);
GO

CREATE TABLE dbo.carts
(
    cart_id      INT IDENTITY(1,1) NOT NULL,
    customer_id  INT               NOT NULL,
    status       VARCHAR(20)       NOT NULL CONSTRAINT DF_carts_status DEFAULT ('open'),
    created_at   DATETIME2(3)      NOT NULL CONSTRAINT DF_carts_created_at DEFAULT (SYSUTCDATETIME()),
    updated_at   DATETIME2(3)      NOT NULL CONSTRAINT DF_carts_updated_at DEFAULT (SYSUTCDATETIME()),

    CONSTRAINT PK_carts PRIMARY KEY (cart_id),
    CONSTRAINT FK_carts_customers FOREIGN KEY (customer_id) REFERENCES dbo.customers (customer_id),
    -- open: being filled; converted: became an order; abandoned: left behind.
    -- "At most one open cart per customer" is enforced by a filtered unique
    -- index in 003_indexes.sql.
    CONSTRAINT CK_carts_status CHECK (status IN ('open', 'converted', 'abandoned'))
);
GO

-- The primary key (cart_id, product_id) means a product appears at most once
-- per cart; adding it again increases the quantity instead.
CREATE TABLE dbo.cart_items
(
    cart_id     INT          NOT NULL,
    product_id  INT          NOT NULL,
    quantity    INT          NOT NULL,
    added_at    DATETIME2(3) NOT NULL CONSTRAINT DF_cart_items_added_at DEFAULT (SYSUTCDATETIME()),

    CONSTRAINT PK_cart_items PRIMARY KEY (cart_id, product_id),
    CONSTRAINT FK_cart_items_carts FOREIGN KEY (cart_id) REFERENCES dbo.carts (cart_id),
    CONSTRAINT FK_cart_items_products FOREIGN KEY (product_id) REFERENCES dbo.products (product_id),
    CONSTRAINT CK_cart_items_quantity CHECK (quantity > 0)
);
GO

-- ===========================================================================
-- Orders and payments
-- ===========================================================================

CREATE TABLE dbo.orders
(
    order_id           INT IDENTITY(1,1) NOT NULL,
    customer_id        INT               NOT NULL,
    cart_id            INT               NULL,      -- the cart it came from; NULL for seeded history
    status             VARCHAR(20)       NOT NULL,
    subtotal           DECIMAL(12,2)     NOT NULL,  -- sum of quantity * unit_price
    shipping_cost      DECIMAL(10,2)     NOT NULL,
    total              DECIMAL(12,2)     NOT NULL,  -- subtotal + shipping_cost
    -- Shipping address is copied at checkout so the order stays correct
    -- if the customer later changes their address.
    ship_name          NVARCHAR(200)     NOT NULL,
    ship_address_line1 NVARCHAR(200)     NOT NULL,
    ship_city          NVARCHAR(100)     NOT NULL,
    ship_state         CHAR(2)           NOT NULL,
    ship_postal_code   VARCHAR(10)       NOT NULL,
    created_at         DATETIME2(3)      NOT NULL CONSTRAINT DF_orders_created_at DEFAULT (SYSUTCDATETIME()),
    updated_at         DATETIME2(3)      NOT NULL CONSTRAINT DF_orders_updated_at DEFAULT (SYSUTCDATETIME()),

    CONSTRAINT PK_orders PRIMARY KEY (order_id),
    CONSTRAINT FK_orders_customers FOREIGN KEY (customer_id) REFERENCES dbo.customers (customer_id),
    CONSTRAINT FK_orders_carts FOREIGN KEY (cart_id) REFERENCES dbo.carts (cart_id),
    -- Lifecycle: pending_payment -> paid -> shipped -> delivered,
    -- or pending_payment -> payment_failed (stock restored), or cancelled.
    CONSTRAINT CK_orders_status CHECK (status IN
        ('pending_payment', 'paid', 'payment_failed', 'shipped', 'delivered', 'cancelled')),
    CONSTRAINT CK_orders_amounts CHECK (subtotal >= 0 AND shipping_cost >= 0 AND total >= 0)
);
GO

-- Surrogate key (order_item_id) on purpose: lookups "by order" and "by
-- product" each get their own index in 003_indexes.sql, so either can be
-- dropped as an incident scenario. A composite clustered key on
-- (order_id, product_id) would make the by-order index permanent.
-- For the same reason there is no UNIQUE (order_id, product_id) constraint:
-- it would create a permanent by-order index. Each product appears once per
-- order anyway, because orders are copied from carts, where
-- PK_cart_items already guarantees it.
CREATE TABLE dbo.order_items
(
    order_item_id  INT IDENTITY(1,1) NOT NULL,
    order_id       INT               NOT NULL,
    product_id     INT               NOT NULL,
    quantity       INT               NOT NULL,
    unit_price     DECIMAL(10,2)     NOT NULL,   -- price at the time of the order, not today's price

    CONSTRAINT PK_order_items PRIMARY KEY (order_item_id),
    CONSTRAINT FK_order_items_orders FOREIGN KEY (order_id) REFERENCES dbo.orders (order_id),
    CONSTRAINT FK_order_items_products FOREIGN KEY (product_id) REFERENCES dbo.products (product_id),
    CONSTRAINT CK_order_items_quantity CHECK (quantity > 0),
    CONSTRAINT CK_order_items_unit_price CHECK (unit_price >= 0)
);
GO

-- One order can have more than one payment attempt, so this is one-to-many.
CREATE TABLE dbo.payments
(
    payment_id          INT IDENTITY(1,1) NOT NULL,
    order_id            INT               NOT NULL,
    amount              DECIMAL(12,2)     NOT NULL,
    method              VARCHAR(20)       NOT NULL,
    status              VARCHAR(20)       NOT NULL,
    provider_reference  VARCHAR(40)       NULL,      -- ID from the (simulated) payment provider
    created_at          DATETIME2(3)      NOT NULL CONSTRAINT DF_payments_created_at DEFAULT (SYSUTCDATETIME()),

    CONSTRAINT PK_payments PRIMARY KEY (payment_id),
    CONSTRAINT FK_payments_orders FOREIGN KEY (order_id) REFERENCES dbo.orders (order_id),
    CONSTRAINT CK_payments_method CHECK (method IN ('card', 'paypal', 'gift_card')),
    CONSTRAINT CK_payments_status CHECK (status IN ('approved', 'declined')),
    CONSTRAINT CK_payments_amount CHECK (amount >= 0)
);
GO

-- ===========================================================================
-- Vendor data
-- ===========================================================================

-- A separate schema marks data that comes from outside ShopLite. In Phase 4
-- the nightly ETL reloads it from the vendor's Postgres database.
-- CREATE SCHEMA must be the only statement in its batch.
CREATE SCHEMA vendor;
GO

-- What each supplier charges ShopLite and how much stock they hold.
-- Several suppliers can offer the same product.
CREATE TABLE vendor.supplier_prices
(
    supplier_price_id  INT IDENTITY(1,1) NOT NULL,
    supplier_name      NVARCHAR(100)     NOT NULL,
    supplier_sku       VARCHAR(40)       NOT NULL,   -- the supplier's own code for the item
    product_id         INT               NOT NULL,
    unit_cost          DECIMAL(10,2)     NOT NULL,   -- ShopLite's cost, below the selling price
    supplier_stock     INT               NOT NULL,
    updated_at         DATETIME2(3)      NOT NULL CONSTRAINT DF_supplier_prices_updated_at DEFAULT (SYSUTCDATETIME()),

    CONSTRAINT PK_supplier_prices PRIMARY KEY (supplier_price_id),
    CONSTRAINT FK_supplier_prices_products FOREIGN KEY (product_id) REFERENCES dbo.products (product_id),
    CONSTRAINT UQ_supplier_prices_supplier_sku UNIQUE (supplier_name, supplier_sku),
    CONSTRAINT CK_supplier_prices_unit_cost CHECK (unit_cost >= 0),
    CONSTRAINT CK_supplier_prices_stock CHECK (supplier_stock >= 0)
);
GO
