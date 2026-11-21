"""Smoke tests for ShopLite's main flows: catalog, cart, checkout, out of
stock, orders, admin reports and restocking, and the synthetic-traffic tag.

"Smoke" means: quick checks that the important paths work end to end against
the real local database. Not exhaustive.
"""

import logging
import math
from decimal import Decimal

import pytest

from app.config import get_settings
from app.pricing import FREE_SHIPPING_FROM, SHIPPING_COST, shipping_cost_for


def stock_of(client, product_id: int) -> int:
    return client.get(f"/api/products/{product_id}").json()["stock_quantity"]


# --- Health and observability ---------------------------------------------------

def test_health_reports_ok_and_version(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": get_settings().app_version, "database": "ok"}


def test_every_response_has_request_id_and_version(client):
    response = client.get("/api/categories", headers={"X-Request-ID": "smoke-test-123"})
    assert response.headers["X-Request-ID"] == "smoke-test-123"       # caller's ID is reused
    assert response.headers["X-App-Version"] == get_settings().app_version
    assert len(client.get("/api/categories").headers["X-Request-ID"]) == 32   # generated when absent


# --- Catalog ---------------------------------------------------------------------

def test_categories_and_product_search(client):
    categories = client.get("/api/categories").json()
    assert len(categories) == 8

    page = client.get("/api/products", params={"category_id": categories[0]["category_id"],
                                               "sort": "price_asc", "page_size": 10}).json()
    prices = [Decimal(p["price"]) for p in page["items"]]
    assert page["total_items"] > 0
    assert prices == sorted(prices)                                    # sort works
    assert all(p["category_id"] == categories[0]["category_id"] for p in page["items"])


def test_product_detail_has_bought_together(client, stocked_product):
    detail = client.get(f"/api/products/{stocked_product['product_id']}").json()
    assert detail["sku"] == stocked_product["sku"]
    assert isinstance(detail["bought_together"], list)
    assert all(p["product_id"] != detail["product_id"] for p in detail["bought_together"])


def test_unknown_product_is_404(client):
    assert client.get("/api/products/999999999").status_code == 404


# --- Account ---------------------------------------------------------------------

def test_login_by_email_and_missing_header_is_401(client):
    customer = client.get("/api/demo-account").json()
    assert client.post("/api/login", json={"email": customer["email"]}).json() == customer
    assert client.post("/api/login", json={"email": "nobody@example.invalid"}).status_code == 404
    assert client.get("/api/cart").status_code == 401                  # no X-Customer-Id


# --- Cart ------------------------------------------------------------------------

def test_cart_add_change_remove(client, shopper, stocked_product):
    pid = stocked_product["product_id"]

    cart = client.post("/api/cart/items", headers=shopper, json={"product_id": pid, "quantity": 2}).json()
    assert cart["items"][0]["quantity"] == 2

    cart = client.post("/api/cart/items", headers=shopper, json={"product_id": pid, "quantity": 1}).json()
    assert cart["items"][0]["quantity"] == 3                           # adding again increases

    cart = client.patch(f"/api/cart/items/{pid}", headers=shopper, json={"quantity": 1}).json()
    assert cart["item_count"] == 1
    assert Decimal(cart["subtotal"]) == Decimal(stocked_product["price"])

    cart = client.delete(f"/api/cart/items/{pid}", headers=shopper).json()
    assert cart["items"] == []


def test_shipping_rule():
    """The one shipping function both cart and checkout use (no database needed)."""
    assert shipping_cost_for(Decimal("0.01")) == SHIPPING_COST
    assert shipping_cost_for(Decimal("74.99")) == SHIPPING_COST
    assert shipping_cost_for(FREE_SHIPPING_FROM) == Decimal("0.00")      # exactly $75 ships free
    assert shipping_cost_for(Decimal("250.00")) == Decimal("0.00")


def test_empty_cart_amounts_are_zero(client, shopper):
    cart = client.get("/api/cart", headers=shopper).json()
    assert (cart["subtotal"], cart["shipping_cost"], cart["estimated_total"]) == ("0.00", "0.00", "0.00")


def test_cart_estimate_follows_shipping_rule(client, shopper, mid_price_product):
    pid, price = mid_price_product["product_id"], Decimal(mid_price_product["price"])

    # One unit: under $75, so flat-rate shipping.
    cart = client.post("/api/cart/items", headers=shopper, json={"product_id": pid, "quantity": 1}).json()
    assert Decimal(cart["shipping_cost"]) == SHIPPING_COST
    assert Decimal(cart["estimated_total"]) == Decimal(cart["subtotal"]) + SHIPPING_COST

    # Enough units to reach $75: free shipping, and the total is just the subtotal.
    units = math.ceil(FREE_SHIPPING_FROM / price)
    cart = client.patch(f"/api/cart/items/{pid}", headers=shopper, json={"quantity": units}).json()
    assert Decimal(cart["subtotal"]) >= FREE_SHIPPING_FROM
    assert Decimal(cart["shipping_cost"]) == Decimal("0.00")
    assert cart["estimated_total"] == cart["subtotal"]


def test_adding_more_than_stock_is_409(client, shopper, scarce_product):
    response = client.post("/api/cart/items", headers=shopper, json={
        "product_id": scarce_product["product_id"], "quantity": scarce_product["stock_quantity"] + 1})
    assert response.status_code == 409
    assert "in stock" in response.json()["detail"]


# --- Checkout --------------------------------------------------------------------

def test_checkout_creates_paid_order_and_reduces_stock(client, shopper, stocked_product):
    pid = stocked_product["product_id"]
    before = stock_of(client, pid)
    cart = client.post("/api/cart/items", headers=shopper, json={"product_id": pid, "quantity": 2}).json()

    response = client.post("/api/checkout", headers=shopper, json={"payment_method": "card"})
    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "paid"
    assert stock_of(client, pid) == before - 2

    order = client.get(f"/api/orders/{result['order_id']}", headers=shopper).json()
    assert Decimal(order["total"]) == Decimal(order["subtotal"]) + Decimal(order["shipping_cost"])
    # Checkout charged exactly what the cart estimated (same shipping function).
    assert order["shipping_cost"] == cart["shipping_cost"]
    assert result["total"] == cart["estimated_total"]
    assert [p["status"] for p in order["payments"]] == ["approved"]
    assert client.get("/api/orders", headers=shopper).json()["items"][0]["order_id"] == result["order_id"]
    assert client.get("/api/cart", headers=shopper).json()["items"] == []   # cart converted


def test_declined_payment_restores_stock(client, shopper, stocked_product, monkeypatch):
    monkeypatch.setattr(get_settings(), "payment_decline_rate", 1.0)    # force a decline
    pid = stocked_product["product_id"]
    before = stock_of(client, pid)
    client.post("/api/cart/items", headers=shopper, json={"product_id": pid, "quantity": 1})

    result = client.post("/api/checkout", headers=shopper, json={}).json()
    assert result["status"] == "payment_failed"
    assert result["payment_status"] == "declined"
    assert stock_of(client, pid) == before                             # back on the shelf


def test_out_of_stock_at_checkout_is_409_and_changes_nothing(client, scarce_product, make_shopper):
    """Two shoppers want the last units; the first buys them, the second gets 409."""
    pid, units = scarce_product["product_id"], scarce_product["stock_quantity"]
    first = make_shopper()
    second = make_shopper(exclude=(int(first["X-Customer-Id"]),))
    for shopper in (first, second):
        assert client.post("/api/cart/items", headers=shopper,
                           json={"product_id": pid, "quantity": units}).status_code == 200

    assert client.post("/api/checkout", headers=first, json={}).status_code == 201
    orders_before = client.get("/api/orders", headers=second).json()["total_items"]

    response = client.post("/api/checkout", headers=second, json={})
    assert response.status_code == 409
    assert "stock" in response.json()["detail"]
    assert stock_of(client, pid) == 0                                  # never below zero
    assert client.get("/api/orders", headers=second).json()["total_items"] == orders_before
    assert len(client.get("/api/cart", headers=second).json()["items"]) == 1   # cart untouched


def test_checkout_with_empty_cart_is_400(client, shopper):
    assert client.post("/api/checkout", headers=shopper, json={}).status_code == 400


# --- Orders ----------------------------------------------------------------------

def test_other_customers_order_is_404(client, shopper, stocked_product, make_shopper):
    client.post("/api/cart/items", headers=shopper, json={"product_id": stocked_product["product_id"]})
    order_id = client.post("/api/checkout", headers=shopper, json={}).json()["order_id"]
    stranger = make_shopper(exclude=(int(shopper["X-Customer-Id"]),))
    assert client.get(f"/api/orders/{order_id}", headers=stranger).status_code == 404


def test_reorder_puts_items_back_in_cart(client, shopper, stocked_product):
    pid = stocked_product["product_id"]
    client.post("/api/cart/items", headers=shopper, json={"product_id": pid, "quantity": 1})
    order_id = client.post("/api/checkout", headers=shopper, json={}).json()["order_id"]

    result = client.post(f"/api/orders/{order_id}/reorder", headers=shopper).json()
    assert result["added"] == 1
    assert result["cart"]["items"][0]["product_id"] == pid


# --- Admin -----------------------------------------------------------------------

def test_admin_requires_token(client):
    assert client.get("/api/admin/inventory").status_code == 401
    assert client.get("/api/admin/inventory", headers={"X-Admin-Token": "wrong-token-123456"}).status_code == 401


def test_reports_return_rows(client, admin_headers):
    sales = client.get("/api/admin/reports/sales", headers=admin_headers,
                       params={"start_date": "2020-01-01", "end_date": "2099-12-31", "group_by": "state"})
    assert sales.status_code == 200
    assert len(sales.json()["rows"]) > 0

    top = client.get("/api/admin/reports/top-products", headers=admin_headers, params={"top": 5})
    assert top.status_code == 200
    assert len(top.json()["rows"]) <= 5


def test_report_procedure_errors_become_400_with_message(client, admin_headers):
    """THROW 50001, 50002, 50003 in the procedures -> HTTP 400 with their message."""
    cases = [
        ("/api/admin/reports/sales", {"start_date": "2026-02-01", "end_date": "2026-01-01"}, "Invalid date range"),
        ("/api/admin/reports/sales", {"group_by": "week"}, "Invalid group_by"),
        ("/api/admin/reports/top-products", {"top": 500}, "Invalid top"),
    ]
    for path, params, message in cases:
        response = client.get(path, headers=admin_headers, params=params)
        assert response.status_code == 400, path
        assert response.json()["detail"].startswith(message)
        assert "[SQL Server]" not in response.json()["detail"]         # driver noise stripped


def test_restock_tops_up_the_most_urgent_product(client, admin_headers):
    """Restock exactly one product (max_products=1), so local data barely changes."""
    low = client.get("/api/admin/inventory", headers=admin_headers,
                     params={"low_stock_only": True, "page_size": 1}).json()["items"]
    if not low:
        pytest.skip("No product at or below its reorder level; nothing to restock")
    most_urgent = low[0]                      # same order as restock: lowest stock first
    target = most_urgent["quantity_on_hand"] + 50

    response = client.post("/api/admin/restock", headers=admin_headers,
                           json={"target_quantity": target, "max_products": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["products_restocked"] == 1
    assert body["target_quantity"] == target
    assert body["units_added"] == target - most_urgent["quantity_on_hand"]
    assert body["still_low"] >= 0


def test_restock_limits_are_enforced(client, admin_headers):
    assert client.post("/api/admin/restock", json={}).status_code == 401          # admin only
    for bad in ({"target_quantity": 0}, {"target_quantity": 1001},
                {"max_products": 0}, {"max_products": 2001},
                {"target_quantity": 50, "typo_field": 1}):                       # unknown field
        assert client.post("/api/admin/restock", headers=admin_headers, json=bad).status_code == 422, bad


# --- Synthetic traffic tag (X-Synthetic-Source) --------------------------------------

def test_cors_preflight_allows_synthetic_source(client):
    """Browsers ask permission before sending a custom header to another origin."""
    origin = get_settings().allowed_origin_list[0]
    response = client.options("/api/cart", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "x-customer-id,x-synthetic-source",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "x-synthetic-source" in response.headers["access-control-allow-headers"].lower()


def test_synthetic_source_is_logged_and_validated(client, caplog):
    """A valid tag appears in the request's log line; an invalid one becomes "-".
    Either way the request is handled exactly the same."""
    request_logger = logging.getLogger("shoplite.request")
    request_logger.addHandler(caplog.handler)    # the API's loggers don't propagate to root
    try:
        tagged = client.get("/health", headers={"X-Synthetic-Source": "playwright/buyer"})
        odd = client.get("/health", headers={"X-Synthetic-Source": "bad tag; <script>"})
    finally:
        request_logger.removeHandler(caplog.handler)

    assert tagged.status_code == odd.status_code == 200
    messages = [record.getMessage() for record in caplog.records if record.name == "shoplite.request"]
    assert any("synthetic playwright/buyer)" in m for m in messages)
    assert any(m.startswith("GET /health") and "synthetic -)" in m for m in messages)
    assert not any("<script>" in m for m in messages)
