"""Shared setup for the ShopLite API smoke tests.

The tests run against your LOCAL, seeded SalesDB using shoplite/api/.env,
and they change data the way real shoppers do (carts, orders, stock).
Run from shoplite/api:  python -m pytest

Never point these at production.
"""

import os
from decimal import Decimal

# Make payments predictable and fast BEFORE the app (and its settings) load:
# real environment variables override shoplite/api/.env. With a 2% random
# decline, a checkout test would fail about 1 run in 50 for no real reason.
# Tests that need a decline switch it on explicitly (see test_smoke.py).
os.environ["PAYMENT_DECLINE_RATE"] = "0"
os.environ["PAYMENT_DELAY_MIN_MS"] = "0"
os.environ["PAYMENT_DELAY_MAX_MS"] = "0"
# Tests never send telemetry, whatever shoplite/api/.env says, and start with
# every feature flag off (tests that need a flag switch it on themselves).
os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = ""
os.environ["FEATURE_FLAGS"] = ""

import pytest  # noqa: E402  (imports after the environment is set, on purpose)
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """A test client that calls the app in-process (no server needed).

    Using it as a context manager runs the app's startup and shutdown
    (flag validation, closing the pool).
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def admin_headers():
    return {"X-Admin-Token": get_settings().admin_token.get_secret_value()}


def empty_cart(client, headers) -> None:
    for item in client.get("/api/cart", headers=headers).json()["items"]:
        client.delete(f"/api/cart/items/{item['product_id']}", headers=headers)


def sign_in_demo_customer(client, exclude: tuple[int, ...] = ()) -> dict:
    """Headers for a random seeded customer (not in exclude), with an empty cart."""
    for _ in range(20):
        customer = client.get("/api/demo-account").json()
        if customer["customer_id"] not in exclude:
            break
    headers = {"X-Customer-Id": str(customer["customer_id"])}
    empty_cart(client, headers)
    return headers


@pytest.fixture
def shopper(client) -> dict:
    """A fresh demo shopper's headers."""
    return sign_in_demo_customer(client)


@pytest.fixture
def make_shopper(client):
    """A function for tests that need more than one shopper:
    make_shopper(exclude=(customer_id, ...)) -> headers.

    Offered as a fixture because tests shouldn't import from conftest.py
    (pytest loads it in its own way; importing it again creates a second copy).
    """
    return lambda exclude=(): sign_in_demo_customer(client, exclude)


@pytest.fixture
def stocked_product(client) -> dict:
    """An active product with at least 10 units in stock."""
    page = 1
    while True:
        body = client.get("/api/products", params={"page": page, "page_size": 60}).json()
        for product in body["items"]:
            if product["stock_quantity"] >= 10:
                return product
        if page >= body["total_pages"]:
            pytest.skip("No product with 10+ units in stock; re-seed the database")
        page += 1


@pytest.fixture
def mid_price_product(client) -> dict:
    """An active product priced $10.00-$74.99 with at least 10 in stock.

    One unit stays under the free-shipping threshold ($75); at most 8 units
    reach it. So a test can check shipping on both sides of the rule.
    """
    page = 1
    while True:
        body = client.get("/api/products", params={"page": page, "page_size": 60, "sort": "price_asc"}).json()
        for product in body["items"]:
            if Decimal("10.00") <= Decimal(product["price"]) <= Decimal("74.99") and product["stock_quantity"] >= 10:
                return product
        if page >= body["total_pages"]:
            pytest.skip("No product priced $10-$74.99 with 10+ in stock; re-seed the database")
        page += 1


@pytest.fixture
def scarce_product(client, admin_headers) -> dict:
    """An active product with only 1 to 20 units left (from the low-stock list).

    The out-of-stock test buys all of it, so each run uses up one scarce
    product; re-seeding restores them.
    """
    page = 1
    while True:
        body = client.get("/api/admin/inventory", headers=admin_headers,
                          params={"low_stock_only": True, "page": page, "page_size": 200}).json()
        for item in body["items"]:
            if 1 <= item["quantity_on_hand"] <= 20:
                detail = client.get(f"/api/products/{item['product_id']}")
                if detail.status_code == 200:          # active, so it can be bought
                    return detail.json()
        if page >= body["total_pages"]:
            pytest.skip("No scarce product left; re-seed the database")
        page += 1
