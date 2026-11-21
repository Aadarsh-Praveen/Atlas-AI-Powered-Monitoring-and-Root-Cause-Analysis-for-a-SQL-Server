"""Cart endpoints: view the open cart, add, change, and remove items.

All endpoints act on the signed-in customer's single OPEN cart
(X-Customer-Id, see account.require_customer). Every change returns the
updated cart.

Stock here is checked but not reserved: the cart only stops shoppers asking
for more than exists right now. Stock is reserved at checkout, which checks
again under lock.

Helpers used by other routers:
  find_open_cart_id  checkout
  read_cart          orders (reorder)
  add_to_cart        orders (reorder)
  ensure_open_cart   orders (reorder)
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import get_engine, violates_unique
from app.pricing import ZERO, shipping_cost_for
from app.routers.account import require_customer
from app.schemas import AddCartItemRequest, Cart, CartItem, ErrorResponse, UpdateCartItemRequest

router = APIRouter(prefix="/api/cart", tags=["cart"])

MAX_QUANTITY_PER_ITEM = 99   # matches the limit in schemas.AddCartItemRequest


# ===========================================================================
# Errors raised by the helpers. Endpoints turn them into HTTP responses;
# reorder catches them to skip an item instead.
# ===========================================================================

class ProductUnavailable(Exception):
    """The product doesn't exist or is discontinued."""


class NotEnoughStock(Exception):
    def __init__(self, name: str, available: int):
        self.name, self.available = name, available
        message = f"{name} is out of stock" if available == 0 else f"Only {available} of {name} in stock"
        super().__init__(message)


class QuantityLimit(Exception):
    def __init__(self):
        super().__init__(f"At most {MAX_QUANTITY_PER_ITEM} of one product per cart")


# ===========================================================================
# Helpers
# ===========================================================================

def find_open_cart_id(conn, customer_id: int) -> int | None:
    """The customer's open cart, if any. A single seek on UX_carts_customer_open."""
    return conn.execute(
        text("SELECT cart_id FROM dbo.carts WHERE customer_id = :customer_id AND status = 'open';"),
        {"customer_id": customer_id},
    ).scalar()


def ensure_open_cart(customer_id: int) -> int:
    """Return the customer's open cart, creating it if needed.

    Carts are created on the first add, so browsing never writes. If two
    requests create the cart at the same moment, the filtered unique index
    UX_carts_customer_open rejects the second INSERT; we catch exactly that
    error and use the cart the other request created.

    The INSERT runs in its own short transaction: with XACT_ABORT ON, a failed
    statement dooms the whole transaction it's in, so it can't be retried
    inside a larger one.
    """
    engine = get_engine()
    with engine.connect() as conn:
        cart_id = find_open_cart_id(conn, customer_id)
    if cart_id is not None:
        return cart_id

    try:
        with engine.begin() as conn:
            return conn.execute(
                text("INSERT INTO dbo.carts (customer_id) OUTPUT INSERTED.cart_id VALUES (:customer_id);"),
                {"customer_id": customer_id},
            ).scalar_one()
    except DBAPIError as error:
        if not violates_unique(error, "UX_carts_customer_open"):
            raise
    # Lost the race: another request created the open cart first.
    with engine.connect() as conn:
        return find_open_cart_id(conn, customer_id)


def read_cart(conn, customer_id: int) -> Cart:
    """The customer's open cart with current prices, stock, and estimated totals."""
    cart_id = find_open_cart_id(conn, customer_id)
    if cart_id is None:
        return empty_cart(cart_id=None)

    rows = conn.execute(text("""
        SELECT ci.product_id, p.sku, p.name, p.price, ci.quantity,
               i.quantity_on_hand AS stock_quantity
        FROM dbo.cart_items AS ci
        JOIN dbo.products  AS p ON p.product_id = ci.product_id
        JOIN dbo.inventory AS i ON i.product_id = ci.product_id
        WHERE ci.cart_id = :cart_id
        ORDER BY ci.added_at, ci.product_id;
    """), {"cart_id": cart_id}).all()

    if not rows:
        return empty_cart(cart_id=cart_id)

    items = [CartItem(**row._mapping, line_total=row.price * row.quantity) for row in rows]
    subtotal = sum((item.line_total for item in items), Decimal("0.00"))
    shipping_cost = shipping_cost_for(subtotal)   # the same function checkout charges with
    return Cart(
        cart_id=cart_id,
        items=items,
        item_count=sum(item.quantity for item in items),
        subtotal=subtotal,
        shipping_cost=shipping_cost,
        estimated_total=subtotal + shipping_cost,
    )


def empty_cart(cart_id: int | None) -> Cart:
    """Nothing in the cart: nothing to ship, so every amount is zero."""
    return Cart(cart_id=cart_id, items=[], item_count=0,
                subtotal=ZERO, shipping_cost=ZERO, estimated_total=ZERO)


def touch_cart(conn, cart_id: int) -> None:
    conn.execute(text("UPDATE dbo.carts SET updated_at = SYSUTCDATETIME() WHERE cart_id = :cart_id;"),
                 {"cart_id": cart_id})


def add_to_cart(conn, cart_id: int, product_id: int, quantity: int) -> None:
    """Add quantity of a product to the cart (or increase it). Call inside a transaction.

    Raises ProductUnavailable, QuantityLimit, or NotEnoughStock; the
    surrounding transaction then rolls back, so nothing changes.
    """
    product = conn.execute(text("""
        SELECT p.name, p.is_active, i.quantity_on_hand
        FROM dbo.products AS p
        JOIN dbo.inventory AS i ON i.product_id = p.product_id
        WHERE p.product_id = :product_id;
    """), {"product_id": product_id}).first()
    if product is None or not product.is_active:
        raise ProductUnavailable(product_id)

    # UPDLOCK, HOLDLOCK: lock this cart line (or, if it doesn't exist yet, the
    # gap where it would go) until the transaction ends. A second add of the
    # same product to the same cart waits here instead of both reading
    # "not there" and both trying to INSERT.
    current = conn.execute(text("""
        SELECT quantity FROM dbo.cart_items WITH (UPDLOCK, HOLDLOCK)
        WHERE cart_id = :cart_id AND product_id = :product_id;
    """), {"cart_id": cart_id, "product_id": product_id}).scalar()

    new_quantity = (current or 0) + quantity
    if new_quantity > MAX_QUANTITY_PER_ITEM:
        raise QuantityLimit()
    if new_quantity > product.quantity_on_hand:
        raise NotEnoughStock(product.name, product.quantity_on_hand)

    if current is None:
        conn.execute(text("""
            INSERT INTO dbo.cart_items (cart_id, product_id, quantity)
            VALUES (:cart_id, :product_id, :quantity);
        """), {"cart_id": cart_id, "product_id": product_id, "quantity": new_quantity})
    else:
        conn.execute(text("""
            UPDATE dbo.cart_items SET quantity = :quantity
            WHERE cart_id = :cart_id AND product_id = :product_id;
        """), {"cart_id": cart_id, "product_id": product_id, "quantity": new_quantity})
    touch_cart(conn, cart_id)


def raise_http_for(error: Exception):
    """Turn a helper error into the matching HTTP error."""
    if isinstance(error, ProductUnavailable):
        raise HTTPException(status_code=404, detail="Product not found or no longer sold")
    if isinstance(error, QuantityLimit):
        raise HTTPException(status_code=400, detail=str(error))
    if isinstance(error, NotEnoughStock):
        raise HTTPException(status_code=409, detail=str(error))
    raise error


# ===========================================================================
# Endpoints
# ===========================================================================

ERRORS = {401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}


@router.get("", response_model=Cart, responses={401: {"model": ErrorResponse}},
            summary="The customer's open cart")
def get_cart(customer_id: int = Depends(require_customer)):
    with get_engine().connect() as conn:
        return read_cart(conn, customer_id)


@router.post("/items", response_model=Cart,
             responses={**ERRORS, 400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
             summary="Add a product to the cart, or increase its quantity")
def add_item(body: AddCartItemRequest, customer_id: int = Depends(require_customer)):
    cart_id = ensure_open_cart(customer_id)
    try:
        with get_engine().begin() as conn:
            add_to_cart(conn, cart_id, body.product_id, body.quantity)
            return read_cart(conn, customer_id)
    except (ProductUnavailable, QuantityLimit, NotEnoughStock) as error:
        raise_http_for(error)


@router.patch("/items/{product_id}", response_model=Cart,
              responses={**ERRORS, 409: {"model": ErrorResponse}},
              summary="Change the quantity of a product in the cart")
def update_item(product_id: int, body: UpdateCartItemRequest, customer_id: int = Depends(require_customer)):
    with get_engine().begin() as conn:
        cart_id = find_open_cart_id(conn, customer_id)
        line = None
        if cart_id is not None:
            # UPDLOCK: hold this line until we've written it, so a concurrent
            # change can't slip in between our read and our update.
            line = conn.execute(text("""
                SELECT p.name, i.quantity_on_hand
                FROM dbo.cart_items AS ci WITH (UPDLOCK)
                JOIN dbo.products  AS p ON p.product_id = ci.product_id
                JOIN dbo.inventory AS i ON i.product_id = ci.product_id
                WHERE ci.cart_id = :cart_id AND ci.product_id = :product_id;
            """), {"cart_id": cart_id, "product_id": product_id}).first()
        if line is None:
            raise HTTPException(status_code=404, detail=f"Product {product_id} is not in your cart")
        if body.quantity > line.quantity_on_hand:
            raise_http_for(NotEnoughStock(line.name, line.quantity_on_hand))

        conn.execute(text("""
            UPDATE dbo.cart_items SET quantity = :quantity
            WHERE cart_id = :cart_id AND product_id = :product_id;
        """), {"cart_id": cart_id, "product_id": product_id, "quantity": body.quantity})
        touch_cart(conn, cart_id)
        return read_cart(conn, customer_id)


@router.delete("/items/{product_id}", response_model=Cart, responses=ERRORS,
               summary="Remove a product from the cart")
def remove_item(product_id: int, customer_id: int = Depends(require_customer)):
    with get_engine().begin() as conn:
        cart_id = find_open_cart_id(conn, customer_id)
        deleted = 0
        if cart_id is not None:
            deleted = conn.execute(text("""
                DELETE FROM dbo.cart_items WHERE cart_id = :cart_id AND product_id = :product_id;
            """), {"cart_id": cart_id, "product_id": product_id}).rowcount
        if deleted == 0:
            raise HTTPException(status_code=404, detail=f"Product {product_id} is not in your cart")
        touch_cart(conn, cart_id)
        return read_cart(conn, customer_id)
