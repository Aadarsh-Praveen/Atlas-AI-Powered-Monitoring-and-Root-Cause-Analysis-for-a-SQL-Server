"""Admin endpoints: inventory and the two sales reports.

!!! DEMO ADMIN ACCESS, NOT REAL AUTHENTICATION.
!!! Every admin request must send X-Admin-Token equal to ADMIN_TOKEN from
!!! shoplite/api/.env. It's one shared secret with no users, roles, or expiry.

The reports call the stored procedures in shoplite/db/005_report_procedures.sql.
They're deliberately heavy queries over order history, running against the
same database that takes live orders. The procedures validate their own
parameters; when they reject them (THROW 50001-50003), this router returns
400 with the procedure's message.
"""

import math
import secrets
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.db import describe_sql_error, get_engine, is_report_input_error
from app.schemas import (ErrorResponse, InventoryItem, Page, SalesReport, SalesReportRow,
                         TopProductRow, TopProductsReport)


def require_admin(x_admin_token: str | None = Header(default=None, description="Demo admin token")) -> None:
    """Dependency: reject the request unless X-Admin-Token matches ADMIN_TOKEN.

    secrets.compare_digest takes the same time whether the first or the last
    character differs, so response timing can't be used to guess the token
    one character at a time.
    """
    expected = get_settings().admin_token.get_secret_value()
    if not x_admin_token or not secrets.compare_digest(x_admin_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Admin token missing or invalid")


# dependencies=[...] applies require_admin to every endpoint in this router.
router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

ADMIN_ERRORS = {401: {"model": ErrorResponse}}
REPORT_ERRORS = {**ADMIN_ERRORS, 400: {"model": ErrorResponse, "description": "Rejected by the report procedure"}}
DEFAULT_REPORT_DAYS = 30


def report_range(start_date: date | None, end_date: date | None) -> tuple[date, date]:
    """Default to the last 30 days ending today (UTC) when dates are omitted."""
    end = end_date or datetime.now(timezone.utc).date()
    start = start_date or end - timedelta(days=DEFAULT_REPORT_DAYS - 1)
    return start, end


def raise_400_if_report_input_error(error: DBAPIError) -> None:
    """Requirement: procedure errors 50001-50003 become HTTP 400 with their message.

    Anything else is re-raised for main.py's database error handling.
    """
    if is_report_input_error(error):
        raise HTTPException(status_code=400, detail=describe_sql_error(error).message)
    raise error


@router.get("/inventory", response_model=Page[InventoryItem], responses=ADMIN_ERRORS,
            summary="Inventory, optionally only items at or below their reorder level")
def inventory(
    low_stock_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    # Comparing two columns (quantity_on_hand <= reorder_level) can't use an
    # index seek, so this scans inventory: fine at one row per product.
    where = "WHERE i.quantity_on_hand <= i.reorder_level" if low_stock_only else ""
    # Low-stock view: most urgent first. Full view: by SKU.
    order_by = "i.quantity_on_hand ASC, p.product_id" if low_stock_only else "p.sku"

    with get_engine().connect() as conn:
        total = conn.execute(text(f"SELECT COUNT(*) FROM dbo.inventory AS i {where};")).scalar_one()
        rows = conn.execute(text(f"""
            SELECT p.product_id, p.sku, p.name, c.name AS category_name,
                   i.quantity_on_hand, i.reorder_level, i.updated_at
            FROM dbo.inventory AS i
            JOIN dbo.products   AS p ON p.product_id  = i.product_id
            JOIN dbo.categories AS c ON c.category_id = p.category_id
            {where}
            ORDER BY {order_by}
            OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY;
        """), {"offset": (page - 1) * page_size, "limit": page_size}).all()

    return Page[InventoryItem](
        items=[InventoryItem(**row._mapping, low_stock=row.quantity_on_hand <= row.reorder_level)
               for row in rows],
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.get("/reports/sales", response_model=SalesReport, responses=REPORT_ERRORS,
            summary="Sales by day, product, or state over a date range")
def sales_report(
    start_date: date | None = Query(default=None, description="Default: 29 days before end_date"),
    end_date: date | None = Query(default=None, description="Default: today (UTC)"),
    # Plain text on purpose: the procedure validates it (THROW 50002), so
    # there is one set of rules and one error message.
    group_by: str = Query(default="day", description="day, product, or state"),
):
    start, end = report_range(start_date, end_date)
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("EXEC dbo.usp_report_sales @start_date = :start_date, "
                     "@end_date = :end_date, @group_by = :group_by;"),
                {"start_date": start, "end_date": end, "group_by": group_by},
            ).all()
    except DBAPIError as error:
        raise_400_if_report_input_error(error)

    return SalesReport(group_by=group_by, start_date=start, end_date=end,
                       rows=[SalesReportRow(**row._mapping) for row in rows])


@router.get("/reports/top-products", response_model=TopProductsReport, responses=REPORT_ERRORS,
            summary="Best sellers by units over a date range")
def top_products_report(
    start_date: date | None = Query(default=None, description="Default: 29 days before end_date"),
    end_date: date | None = Query(default=None, description="Default: today (UTC)"),
    # No range check here: the procedure enforces 1-100 (THROW 50003).
    top: int = Query(default=10, description="How many products, 1 to 100"),
):
    start, end = report_range(start_date, end_date)
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("EXEC dbo.usp_report_top_products @start_date = :start_date, "
                     "@end_date = :end_date, @top = :top;"),
                {"start_date": start, "end_date": end, "top": top},
            ).all()
    except DBAPIError as error:
        raise_400_if_report_input_error(error)

    return TopProductsReport(start_date=start, end_date=end, top=top,
                             rows=[TopProductRow(**row._mapping) for row in rows])
