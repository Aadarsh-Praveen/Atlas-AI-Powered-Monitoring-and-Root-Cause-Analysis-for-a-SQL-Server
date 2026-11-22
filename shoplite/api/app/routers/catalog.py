"""Catalog endpoints: aisles, product search, and product detail.

These are the most frequently called queries in ShopLite: every browse and
search click lands here. Each query names the index it relies on (see
shoplite/db/003_indexes.sql), because Phase 5 drops some of those indexes.
"""

import math

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from app import flags, ranking
from app.db import get_engine
from app.schemas import Category, ErrorResponse, Page, ProductDetail, ProductSort, ProductSummary

router = APIRouter(prefix="/api", tags=["catalog"])

BOUGHT_TOGETHER_LIMIT = 6

# The columns behind ProductSummary, shared by every product query.
PRODUCT_COLUMNS = """
    p.product_id, p.sku, p.name, p.brand, p.price, p.category_id,
    c.name AS category_name,
    i.quantity_on_hand AS stock_quantity, i.reorder_level
"""
PRODUCT_JOINS = """
    FROM dbo.products AS p
    JOIN dbo.categories AS c ON c.category_id = p.category_id
    JOIN dbo.inventory  AS i ON i.product_id  = p.product_id
"""

# Sort options map to fixed ORDER BY text. User input only ever selects a key
# here; it never becomes part of the SQL. product_id breaks ties so paging is
# stable (the same product can't appear on two pages).
SORT_ORDER = {
    "name":       "p.name ASC, p.product_id ASC",
    "price_asc":  "p.price ASC, p.product_id ASC",
    "price_desc": "p.price DESC, p.product_id ASC",
    "newest":     "p.created_at DESC, p.product_id DESC",
}


def to_summary(row) -> ProductSummary:
    """Build a ProductSummary from a row, adding the stock flags."""
    data = dict(row._mapping)
    quantity, reorder_level = data["stock_quantity"], data.pop("reorder_level")
    return ProductSummary(
        **{k: v for k, v in data.items() if k in ProductSummary.model_fields},
        in_stock=quantity > 0,
        low_stock=0 < quantity <= reorder_level,
    )


def like_pattern(term: str) -> str:
    """Turn a search term into a LIKE pattern that matches it literally.

    In LIKE, % and _ are wildcards and [ starts a character range. Escape them
    (with '!', declared by ESCAPE '!' in the SQL) so "100%" searches for the
    text "100%", not "100 followed by anything".
    """
    # "!" itself is escaped first, so later replacements don't double it.
    escaped = (term.replace("!", "!!").replace("%", "!%")
                   .replace("_", "!_").replace("[", "!["))
    return f"%{escaped}%"


@router.get("/categories", response_model=list[Category], summary="List the aisles")
def list_categories():
    with get_engine().connect() as conn:
        rows = conn.execute(text("""
            SELECT category_id, name, slug, description
            FROM dbo.categories
            ORDER BY sort_order, name;
        """)).all()
    return [Category(**row._mapping) for row in rows]


@router.get("/products", response_model=Page[ProductSummary],
            summary="Search, filter, sort, and page through products")
def list_products(
    q: str | None = Query(default=None, max_length=100, description="Search name or SKU"),
    category_id: int | None = Query(default=None, gt=0, description="Only this aisle"),
    sort: ProductSort = Query(default="name"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=60),
):
    # Build the WHERE clause from fixed fragments; values go in as parameters.
    conditions = ["p.is_active = 1"]
    params: dict = {}
    if category_id is not None:
        # Served by IX_products_category_id (category_id, is_active).
        conditions.append("p.category_id = :category_id")
        params["category_id"] = category_id
    if q and q.strip():
        # A leading-wildcard LIKE can't seek an index, so search scans the
        # products table. Acceptable at 5,000 rows (see 003_indexes.sql).
        conditions.append("(p.name LIKE :pattern ESCAPE '!' OR p.sku LIKE :pattern ESCAPE '!')")
        params["pattern"] = like_pattern(q.strip())
    where = " AND ".join(conditions)

    with get_engine().connect() as conn:
        total = conn.execute(text(f"SELECT COUNT(*) {PRODUCT_JOINS} WHERE {where};"), params).scalar_one()
        rows = conn.execute(text(f"""
            SELECT {PRODUCT_COLUMNS}
            {PRODUCT_JOINS}
            WHERE {where}
            ORDER BY {SORT_ORDER[sort]}
            OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY;
        """), {**params, "offset": (page - 1) * page_size, "limit": page_size}).all()

    items = [to_summary(row) for row in rows]
    # Search ranking v2 (feature flag, off by default): searches with the
    # default sort come back in relevance order from the ranking service.
    if q and q.strip() and sort == "name" and flags.is_enabled("search_ranking_v2"):
        items = ranking.rerank(items, q.strip())

    return Page[ProductSummary](
        items=items,
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.get("/products/{product_id}", response_model=ProductDetail,
            responses={404: {"model": ErrorResponse}},
            summary="One product with stock and 'often bought together'")
def get_product(product_id: int):
    with get_engine().connect() as conn:
        row = conn.execute(text(f"""
            SELECT {PRODUCT_COLUMNS},
                   p.description, c.slug AS category_slug, p.created_at
            {PRODUCT_JOINS}
            WHERE p.product_id = :product_id AND p.is_active = 1;
        """), {"product_id": product_id}).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Product {product_id} not found")

        # "Often bought together": other products in the same orders as this
        # one, ranked by how many orders they share. Uses both order_items
        # indexes: IX_order_items_product_id finds this product's orders, then
        # IX_order_items_order_id finds the other items in those orders.
        # For a best seller this touches thousands of orders: a realistic
        # cost that Phase 5 can make much worse by dropping either index.
        together = conn.execute(text(f"""
            SELECT TOP (:limit) {PRODUCT_COLUMNS}, COUNT(*) AS times_together
            FROM dbo.order_items AS mine
            JOIN dbo.order_items AS other
                 ON other.order_id = mine.order_id
                AND other.product_id <> mine.product_id
            JOIN dbo.products   AS p ON p.product_id  = other.product_id
            JOIN dbo.categories AS c ON c.category_id = p.category_id
            JOIN dbo.inventory  AS i ON i.product_id  = p.product_id
            WHERE mine.product_id = :product_id
              AND p.is_active = 1
            GROUP BY p.product_id, p.sku, p.name, p.brand, p.price, p.category_id,
                     c.name, i.quantity_on_hand, i.reorder_level
            ORDER BY times_together DESC, p.product_id;
        """), {"product_id": product_id, "limit": BOUGHT_TOGETHER_LIMIT}).all()

    summary = to_summary(row)
    return ProductDetail(
        **summary.model_dump(),
        description=row.description,
        category_slug=row.category_slug,
        created_at=row.created_at,
        bought_together=[to_summary(r) for r in together],
    )
