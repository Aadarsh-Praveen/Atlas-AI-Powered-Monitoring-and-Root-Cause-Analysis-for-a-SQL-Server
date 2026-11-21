"""Request and response models (Pydantic) for the ShopLite API.

FastAPI uses these to:
  - validate request bodies and reject bad input with a 422 before any of our
    code runs,
  - send only the listed fields in responses,
  - generate the interactive docs at /docs.
The storefront (and later the Playwright shoppers) code against these shapes,
so changing a field is a breaking change.

Conventions:
  - Money is Decimal and is sent as a JSON string ("24.99"), so it stays
    exact from the database to the browser. Never float.
  - Timestamps are UTC and are sent with a "Z" suffix (see UtcDateTime).
  - Request models forbid unknown fields, so a misspelled field is a 422
    instead of being silently ignored.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _as_utc(value: datetime) -> datetime:
    """SalesDB stores UTC in DATETIME2, which has no time zone. Attach UTC so
    the JSON says so ("...Z"); otherwise browsers would read it as local time."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


UtcDateTime = Annotated[datetime, AfterValidator(_as_utc)]
Money = Decimal

PaymentMethod = Literal["card", "paypal", "gift_card"]
OrderStatus = Literal["pending_payment", "paid", "payment_failed", "shipped", "delivered", "cancelled"]
ProductSort = Literal["name", "price_asc", "price_desc", "newest"]
SalesGrouping = Literal["day", "product", "state"]

T = TypeVar("T")


class RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected."""
    model_config = ConfigDict(extra="forbid")


class Page(BaseModel, Generic[T]):
    """One page of a longer list. Page numbers start at 1."""
    items: list[T]
    page: int
    page_size: int
    total_items: int
    total_pages: int


class ErrorResponse(BaseModel):
    """Shape of every error body (documented in /docs)."""
    detail: str


# ===========================================================================
# Health
# ===========================================================================

class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    version: str
    database: Literal["ok", "unreachable"]


# ===========================================================================
# Catalog
# ===========================================================================

class Category(BaseModel):
    category_id: int
    name: str
    slug: str
    description: str | None


class ProductSummary(BaseModel):
    """What a product tile and its yellow shelf tag show."""
    product_id: int
    sku: str
    name: str
    brand: str | None
    price: Money
    category_id: int
    category_name: str
    stock_quantity: int
    in_stock: bool
    low_stock: bool          # at or below the reorder level, but not zero


class ProductDetail(ProductSummary):
    description: str | None
    category_slug: str
    created_at: UtcDateTime
    # Products that appear most often in the same orders as this one.
    bought_together: list[ProductSummary]


# ===========================================================================
# Account (demo sign-in, not real authentication)
# ===========================================================================

class LoginRequest(RequestModel):
    email: str = Field(min_length=3, max_length=256, examples=["james.smith1@example.com"])


class Customer(BaseModel):
    customer_id: int
    email: str
    first_name: str
    last_name: str
    phone: str | None
    address_line1: str
    city: str
    state: str
    postal_code: str


# ===========================================================================
# Cart
# ===========================================================================

class CartItem(BaseModel):
    product_id: int
    sku: str
    name: str
    price: Money             # current price; the order captures it at checkout
    quantity: int
    line_total: Money
    stock_quantity: int


class Cart(BaseModel):
    cart_id: int | None      # None until the customer adds their first item
    items: list[CartItem]
    item_count: int          # total units, for the header badge
    subtotal: Money
    # Calculated by app/pricing.py, the same rule checkout charges. "Estimated"
    # because prices or stock can change before the order is placed; checkout
    # recalculates and its result is final.
    shipping_cost: Money
    estimated_total: Money


class AddCartItemRequest(RequestModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(default=1, ge=1, le=99)


class UpdateCartItemRequest(RequestModel):
    quantity: int = Field(ge=1, le=99)


# ===========================================================================
# Checkout
# ===========================================================================

class ShippingAddress(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address_line1: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(pattern=r"^[A-Z]{2}$")          # US state code
    postal_code: str = Field(pattern=r"^\d{5}$")


class CheckoutRequest(RequestModel):
    payment_method: PaymentMethod = "card"
    # Omit to ship to the customer's address on file.
    shipping: ShippingAddress | None = None


class CheckoutResult(BaseModel):
    """Returned with 201 whether the payment was approved or declined: either
    way an order now exists. Out of stock is a 409 and creates nothing."""
    order_id: int
    status: Literal["paid", "payment_failed"]
    payment_status: Literal["approved", "declined"]
    total: Money
    message: str


# ===========================================================================
# Orders
# ===========================================================================

class OrderSummary(BaseModel):
    order_id: int
    created_at: UtcDateTime
    status: OrderStatus
    total: Money


class OrderItem(BaseModel):
    product_id: int
    sku: str
    name: str
    quantity: int
    unit_price: Money        # price when ordered, not today's
    line_total: Money


class Payment(BaseModel):
    method: PaymentMethod
    status: Literal["approved", "declined"]
    amount: Money
    created_at: UtcDateTime


class OrderDetail(BaseModel):
    order_id: int
    created_at: UtcDateTime
    updated_at: UtcDateTime
    status: OrderStatus
    subtotal: Money
    shipping_cost: Money
    total: Money
    shipping: ShippingAddress
    items: list[OrderItem]
    payments: list[Payment]


class SkippedItem(BaseModel):
    product_id: int
    name: str
    reason: str


class ReorderResult(BaseModel):
    cart: Cart
    added: int                    # number of products put back in the cart
    skipped: list[SkippedItem]    # discontinued or out of stock


# ===========================================================================
# Admin
# ===========================================================================

class InventoryItem(BaseModel):
    product_id: int
    sku: str
    name: str
    category_name: str
    quantity_on_hand: int
    reorder_level: int
    low_stock: bool
    updated_at: UtcDateTime


class SalesReportRow(BaseModel):
    """One group in the sales report. Which label fields are set depends on
    group_by: sales_date (day), product_id/sku/product_name (product), or
    state (state)."""
    sales_date: date | None = None
    product_id: int | None = None
    sku: str | None = None
    product_name: str | None = None
    state: str | None = None
    order_count: int
    units_sold: int
    revenue: Money


class SalesReport(BaseModel):
    group_by: SalesGrouping
    start_date: date
    end_date: date
    rows: list[SalesReportRow]


class TopProductRow(BaseModel):
    product_id: int
    sku: str
    product_name: str
    category_name: str
    units_sold: int
    revenue: Money
    order_count: int


class TopProductsReport(BaseModel):
    start_date: date
    end_date: date
    top: int
    rows: list[TopProductRow]
