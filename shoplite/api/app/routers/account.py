"""Account endpoints: demo sign-in, and the X-Customer-Id dependency.

!!! DEMO SIGN-IN, NOT REAL AUTHENTICATION.
!!! Anyone who knows (or guesses) an email or a customer ID can act as that
!!! customer. There are no passwords and no sessions. This is acceptable only
!!! because ShopLite's customers are seeded test data and its shoppers are
!!! simulated (Phase 3). Never reuse this for real users.

How it works:
  1. The storefront calls POST /api/login (or GET /api/demo-account) and gets
     a customer back.
  2. It remembers customer_id and sends it as the X-Customer-Id header on
     cart, checkout, and order requests.
  3. Those endpoints depend on require_customer, which checks the header.
"""

import random

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import text

from app.db import get_engine
from app.schemas import Customer, ErrorResponse, LoginRequest

router = APIRouter(prefix="/api", tags=["account"])

MAX_SQL_INT = 2_147_483_647   # largest value an INT column can hold

CUSTOMER_COLUMNS = """
    customer_id, email, first_name, last_name, phone,
    address_line1, city, state, postal_code
"""


def require_customer(
    x_customer_id: str | None = Header(default=None, description="Signed-in demo customer"),
) -> int:
    """FastAPI dependency: the customer making this request, from X-Customer-Id.

    Use it as a parameter:  customer_id: int = Depends(require_customer)

    The header is read as text (not int) so that a missing or malformed value
    gets a clear 401 from us, rather than FastAPI's generic 422.
    """
    # ASCII digits only (str.isdigit alone also accepts characters like "²"),
    # and within SQL Server's INT range, so bad input is a 401, never a 500.
    if not x_customer_id or not (x_customer_id.isascii() and x_customer_id.isdigit()):
        raise HTTPException(status_code=401, detail="Sign in first: X-Customer-Id header is missing or invalid")
    customer_id = int(x_customer_id)
    if not 1 <= customer_id <= MAX_SQL_INT:
        raise HTTPException(status_code=401, detail="Sign in first: X-Customer-Id header is missing or invalid")

    # One primary-key seek. Rejecting unknown IDs here means a forged or stale
    # ID gets a clear 401, not a foreign key error (500) deep inside checkout.
    with get_engine().connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM dbo.customers WHERE customer_id = :customer_id;"),
            {"customer_id": customer_id},
        ).first()
    if exists is None:
        raise HTTPException(status_code=401, detail=f"Unknown customer {customer_id}; sign in again")
    return customer_id


@router.post("/login", response_model=Customer, responses={404: {"model": ErrorResponse}},
             summary="Demo sign-in by email (not real authentication)")
def login(body: LoginRequest):
    with get_engine().connect() as conn:
        # Seeks UQ_customers_email's index. The database's default collation
        # is case-insensitive, so letter case in the email doesn't matter.
        row = conn.execute(
            text(f"SELECT {CUSTOMER_COLUMNS} FROM dbo.customers WHERE email = :email;"),
            {"email": body.email.strip()},
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No customer has that email")
    return Customer(**row._mapping)


@router.get("/demo-account", response_model=Customer,
            summary="A random seeded customer to sign in as (used by simulated shoppers)")
def demo_account():
    with get_engine().connect() as conn:
        # ORDER BY NEWID() would sort every customer to pick one. Instead:
        # pick a random number up to the highest ID and take the first
        # customer at or above it. Two primary-key seeks at any table size.
        highest = conn.execute(text("SELECT MAX(customer_id) FROM dbo.customers;")).scalar_one()
        if highest is None:
            raise HTTPException(status_code=404, detail="No customers yet; run shoplite/db/seed.py")
        row = conn.execute(text(f"""
            SELECT TOP (1) {CUSTOMER_COLUMNS}
            FROM dbo.customers
            WHERE customer_id >= :start
            ORDER BY customer_id;
        """), {"start": random.randint(1, highest)}).first()
    return Customer(**row._mapping)
