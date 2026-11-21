"""POST /api/checkout: turn the open cart into an order and take payment.

The design (important for later incidents):

  1. Transaction 1, kept SHORT: lock the cart and its inventory rows, check
     stock, create the order and its items, reduce stock, mark the cart
     converted. Not enough stock: 409, and nothing changes.
  2. AFTER transaction 1 commits: call the (simulated) payment provider.
     It takes a few hundred milliseconds and sometimes declines.
  3. Transaction 2, also short: record the payment and set the order status.
     A declined payment restores the stock.

Calling the payment provider OUTSIDE any transaction is the correct design:
no locks are held while waiting on a slow external service. A Phase 5
scenario moves the call inside transaction 1. Then every checkout holds its
inventory locks for the whole payment delay, other checkouts (and, with RCSI
off, catalog reads of those rows) queue behind it, and Atlas has to work out
why.
"""

import logging
import random
import secrets
import time
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.db import get_engine, violates_check_constraint
from app.pricing import shipping_cost_for
from app.routers.account import require_customer
from app.schemas import CheckoutRequest, CheckoutResult, ErrorResponse, ShippingAddress

logger = logging.getLogger("shoplite.checkout")

router = APIRouter(prefix="/api", tags=["checkout"])

OUT_OF_STOCK_CONSTRAINT = "CK_inventory_quantity"


class OutOfStock(Exception):
    """Raised inside transaction 1 to roll it back; becomes a 409."""


# ===========================================================================
# Simulated payment provider
# ===========================================================================

def call_payment_provider(amount: Decimal, method: str) -> tuple[bool, str]:
    """Pretend to charge the customer. Returns (approved, provider_reference).

    Like a real provider it's slow (a random delay) and sometimes says no.
    It must never be called while a database transaction is open.
    """
    settings = get_settings()
    delay_ms = random.uniform(settings.payment_delay_min_ms, settings.payment_delay_max_ms)
    time.sleep(delay_ms / 1000)
    approved = random.random() >= settings.payment_decline_rate
    return approved, f"SIM-{secrets.token_hex(6)}"


# ===========================================================================
# Steps
# ===========================================================================

def shipping_for(conn, customer_id: int, requested: ShippingAddress | None) -> ShippingAddress:
    """The address from the request, or the customer's address on file."""
    if requested is not None:
        return requested
    row = conn.execute(text("""
        SELECT first_name + N' ' + last_name AS name, address_line1, city, state, postal_code
        FROM dbo.customers WHERE customer_id = :customer_id;
    """), {"customer_id": customer_id}).one()
    return ShippingAddress(**row._mapping)


def place_order(conn, customer_id: int, shipping: ShippingAddress) -> tuple[int, Decimal, list]:
    """Transaction 1: everything that must happen together, nothing slow.

    Returns (order_id, total, items). Raises HTTPException(400) for an empty
    cart and OutOfStock when stock is short; either way the caller's
    transaction rolls back and nothing changes.
    """
    # Lock the open cart row. If the shopper double-clicks "Place order", the
    # second request waits here, then finds the cart already converted.
    cart_id = conn.execute(text("""
        SELECT cart_id FROM dbo.carts WITH (UPDLOCK)
        WHERE customer_id = :customer_id AND status = 'open';
    """), {"customer_id": customer_id}).scalar()
    if cart_id is None:
        raise HTTPException(status_code=400, detail="Your cart is empty")

    # Lock this cart's inventory rows with UPDLOCK: other checkouts for the
    # same products wait here until we commit, so two shoppers can't both buy
    # the last unit. ROWLOCK asks for row locks only, not page or table locks.
    # ORDER BY product_id makes every checkout lock rows in the same order,
    # which lowers the chance of deadlocks (it helps; it isn't a guarantee).
    items = conn.execute(text("""
        SELECT ci.product_id, ci.quantity, p.name, p.price, p.is_active, i.quantity_on_hand
        FROM dbo.cart_items AS ci
        JOIN dbo.products  AS p ON p.product_id = ci.product_id
        JOIN dbo.inventory AS i WITH (UPDLOCK, ROWLOCK) ON i.product_id = ci.product_id
        WHERE ci.cart_id = :cart_id
        ORDER BY ci.product_id;
    """), {"cart_id": cart_id}).all()
    if not items:
        raise HTTPException(status_code=400, detail="Your cart is empty")

    problems = []
    for item in items:
        if not item.is_active:
            problems.append(f"{item.name} is no longer sold")
        elif item.quantity > item.quantity_on_hand:
            problems.append(f"{item.name} is out of stock" if item.quantity_on_hand == 0
                            else f"Only {item.quantity_on_hand} of {item.name} in stock")
    if problems:
        raise OutOfStock("; ".join(problems))

    subtotal = sum((item.price * item.quantity for item in items), Decimal("0.00"))
    # Same function the cart uses for its estimate (app/pricing.py).
    shipping_cost = shipping_cost_for(subtotal)
    total = subtotal + shipping_cost

    order_id = conn.execute(text("""
        INSERT INTO dbo.orders
            (customer_id, cart_id, status, subtotal, shipping_cost, total,
             ship_name, ship_address_line1, ship_city, ship_state, ship_postal_code)
        OUTPUT INSERTED.order_id
        VALUES
            (:customer_id, :cart_id, 'pending_payment', :subtotal, :shipping_cost, :total,
             :ship_name, :ship_address_line1, :ship_city, :ship_state, :ship_postal_code);
    """), {
        "customer_id": customer_id, "cart_id": cart_id,
        "subtotal": subtotal, "shipping_cost": shipping_cost, "total": total,
        "ship_name": shipping.name, "ship_address_line1": shipping.address_line1,
        "ship_city": shipping.city, "ship_state": shipping.state,
        "ship_postal_code": shipping.postal_code,
    }).scalar_one()

    # Passing a list of parameter sets runs the statement once per item.
    conn.execute(text("""
        INSERT INTO dbo.order_items (order_id, product_id, quantity, unit_price)
        VALUES (:order_id, :product_id, :quantity, :unit_price);
    """), [{"order_id": order_id, "product_id": item.product_id,
            "quantity": item.quantity, "unit_price": item.price} for item in items])

    # Reduce stock. The CHECK constraint CK_inventory_quantity (>= 0) is the
    # last line of defence if anything ever gets past the check above.
    conn.execute(text("""
        UPDATE dbo.inventory
        SET quantity_on_hand = quantity_on_hand - :quantity, updated_at = SYSUTCDATETIME()
        WHERE product_id = :product_id;
    """), [{"product_id": item.product_id, "quantity": item.quantity} for item in items])

    conn.execute(text("""
        UPDATE dbo.carts SET status = 'converted', updated_at = SYSUTCDATETIME()
        WHERE cart_id = :cart_id;
    """), {"cart_id": cart_id})

    return order_id, total, items


def record_payment(conn, order_id: int, total: Decimal, method: str,
                   approved: bool, reference: str, items: list) -> None:
    """Transaction 2: record the payment outcome; on decline, restore stock."""
    conn.execute(text("""
        INSERT INTO dbo.payments (order_id, amount, method, status, provider_reference)
        VALUES (:order_id, :amount, :method, :status, :reference);
    """), {"order_id": order_id, "amount": total, "method": method,
           "status": "approved" if approved else "declined", "reference": reference})

    conn.execute(text("""
        UPDATE dbo.orders SET status = :status, updated_at = SYSUTCDATETIME()
        WHERE order_id = :order_id AND status = 'pending_payment';
    """), {"order_id": order_id, "status": "paid" if approved else "payment_failed"})

    if not approved:
        # Put the units back on the shelf, in the same product order as
        # transaction 1 took its locks.
        conn.execute(text("""
            UPDATE dbo.inventory
            SET quantity_on_hand = quantity_on_hand + :quantity, updated_at = SYSUTCDATETIME()
            WHERE product_id = :product_id;
        """), [{"product_id": item.product_id, "quantity": item.quantity}
               for item in sorted(items, key=lambda i: i.product_id)])


# ===========================================================================
# Endpoint
# ===========================================================================

@router.post(
    "/checkout",
    status_code=201,
    response_model=CheckoutResult,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse},
               409: {"model": ErrorResponse, "description": "Not enough stock; nothing changed"}},
    summary="Place an order from the cart and take payment",
)
def checkout(body: CheckoutRequest, customer_id: int = Depends(require_customer)):
    engine = get_engine()

    # Step 1: one short transaction. Commits when the block ends.
    try:
        with engine.begin() as conn:
            shipping = shipping_for(conn, customer_id, body.shipping)
            order_id, total, items = place_order(conn, customer_id, shipping)
    except OutOfStock as error:
        raise HTTPException(status_code=409, detail=str(error))
    except DBAPIError as error:
        # Backstop: if the stock UPDATE ever violates CK_inventory_quantity
        # (stock would go below zero), XACT_ABORT has already rolled the
        # whole transaction back. Tell the shopper "out of stock", not "500".
        if violates_check_constraint(error, OUT_OF_STOCK_CONSTRAINT):
            logger.warning("Checkout hit %s for customer %s; returned 409",
                           OUT_OF_STOCK_CONSTRAINT, customer_id)
            raise HTTPException(status_code=409,
                                detail="Sorry, an item in your cart just went out of stock. "
                                       "Please review your cart.")
        raise

    # Step 2: the slow external call, with NO transaction open and no locks held.
    # If the API crashed right here, the order would stay pending_payment with
    # its stock reserved; a real shop reconciles those with a scheduled job.
    approved, reference = call_payment_provider(total, body.payment_method)

    # Step 3: a second short transaction.
    with engine.begin() as conn:
        record_payment(conn, order_id, total, body.payment_method, approved, reference, items)

    if approved:
        logger.info("Order %s paid: %s by %s", order_id, total, body.payment_method)
        return CheckoutResult(order_id=order_id, status="paid", payment_status="approved",
                              total=total, message=f"Thank you! Order #{order_id} is confirmed.")

    logger.info("Order %s payment declined; stock restored", order_id)
    return CheckoutResult(
        order_id=order_id, status="payment_failed", payment_status="declined", total=total,
        message=(f"Your payment was declined, so order #{order_id} was not charged and the items "
                 "are back on the shelf. You can try again from your order history (Reorder)."),
    )
