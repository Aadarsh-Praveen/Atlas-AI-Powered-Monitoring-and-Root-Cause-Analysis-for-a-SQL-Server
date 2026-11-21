"""Order endpoints: history, one order, and reorder.

Customers only ever see their own orders. Asking for someone else's order
returns 404, exactly as if it didn't exist, so order IDs can't be probed.
"""

import math

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app.db import get_engine
from app.routers.account import require_customer
from app.routers.cart import (NotEnoughStock, ProductUnavailable, QuantityLimit, add_to_cart,
                              ensure_open_cart, read_cart)
from app.schemas import (ErrorResponse, OrderDetail, OrderItem, OrderSummary, Page, Payment,
                         ReorderResult, ShippingAddress, SkippedItem)

router = APIRouter(prefix="/api/orders", tags=["orders"])

NOT_FOUND = {404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}}


def order_items(conn, order_id: int) -> list:
    """The items of one order. Served by IX_order_items_order_id."""
    return conn.execute(text("""
        SELECT oi.product_id, p.sku, p.name, oi.quantity, oi.unit_price
        FROM dbo.order_items AS oi
        JOIN dbo.products AS p ON p.product_id = oi.product_id
        WHERE oi.order_id = :order_id
        ORDER BY oi.order_item_id;
    """), {"order_id": order_id}).all()


def owned_order(conn, order_id: int, customer_id: int):
    """The order row if it belongs to this customer, else None."""
    return conn.execute(text("""
        SELECT order_id, created_at, updated_at, status, subtotal, shipping_cost, total,
               ship_name, ship_address_line1, ship_city, ship_state, ship_postal_code
        FROM dbo.orders
        WHERE order_id = :order_id AND customer_id = :customer_id;
    """), {"order_id": order_id, "customer_id": customer_id}).first()


@router.get("", response_model=Page[OrderSummary], responses={401: {"model": ErrorResponse}},
            summary="The customer's order history, newest first")
def list_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    customer_id: int = Depends(require_customer),
):
    with get_engine().connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM dbo.orders WHERE customer_id = :customer_id;"),
                             {"customer_id": customer_id}).scalar_one()
        # Served entirely by IX_orders_customer_id_created_at: seek to the
        # customer, read already sorted by created_at DESC, and status/total
        # come from the index's INCLUDE columns. Phase 5 drops this index.
        rows = conn.execute(text("""
            SELECT order_id, created_at, status, total
            FROM dbo.orders
            WHERE customer_id = :customer_id
            ORDER BY created_at DESC, order_id DESC
            OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY;
        """), {"customer_id": customer_id, "offset": (page - 1) * page_size, "limit": page_size}).all()

    return Page[OrderSummary](
        items=[OrderSummary(**row._mapping) for row in rows],
        page=page,
        page_size=page_size,
        total_items=total,
        total_pages=math.ceil(total / page_size) if total else 0,
    )


@router.get("/{order_id}", response_model=OrderDetail, responses=NOT_FOUND,
            summary="One order with its items and payments")
def get_order(order_id: int, customer_id: int = Depends(require_customer)):
    with get_engine().connect() as conn:
        order = owned_order(conn, order_id, customer_id)
        if order is None:
            raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
        items = order_items(conn, order_id)
        # Served by IX_payments_order_id.
        payments = conn.execute(text("""
            SELECT method, status, amount, created_at
            FROM dbo.payments
            WHERE order_id = :order_id
            ORDER BY created_at, payment_id;
        """), {"order_id": order_id}).all()

    return OrderDetail(
        order_id=order.order_id,
        created_at=order.created_at,
        updated_at=order.updated_at,
        status=order.status,
        subtotal=order.subtotal,
        shipping_cost=order.shipping_cost,
        total=order.total,
        shipping=ShippingAddress(
            name=order.ship_name, address_line1=order.ship_address_line1,
            city=order.ship_city, state=order.ship_state, postal_code=order.ship_postal_code,
        ),
        items=[OrderItem(**item._mapping, line_total=item.unit_price * item.quantity) for item in items],
        payments=[Payment(**p._mapping) for p in payments],
    )


@router.post("/{order_id}/reorder", response_model=ReorderResult, responses=NOT_FOUND,
             summary="Put a past order's items back in the cart")
def reorder(order_id: int, customer_id: int = Depends(require_customer)):
    with get_engine().connect() as conn:
        if owned_order(conn, order_id, customer_id) is None:
            raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
        items = order_items(conn, order_id)

    cart_id = ensure_open_cart(customer_id)
    added, skipped = 0, []
    with get_engine().begin() as conn:
        for item in items:
            # add_to_cart checks before it writes, so these errors are raised
            # by Python before any SQL fails: the transaction stays usable and
            # the other items can still be added.
            try:
                add_to_cart(conn, cart_id, item.product_id, item.quantity)
                added += 1
            except ProductUnavailable:
                skipped.append(SkippedItem(product_id=item.product_id, name=item.name,
                                           reason="No longer sold"))
            except (NotEnoughStock, QuantityLimit) as error:
                skipped.append(SkippedItem(product_id=item.product_id, name=item.name,
                                           reason=str(error)))
        cart = read_cart(conn, customer_id)

    return ReorderResult(cart=cart, added=added, skipped=skipped)
