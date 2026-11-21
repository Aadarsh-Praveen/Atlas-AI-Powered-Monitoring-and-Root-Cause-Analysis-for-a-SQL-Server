"""Locust: background load on the ShopLite API.

The Playwright shoppers are realistic but expensive (a whole browser each).
Locust calls the API directly, so a handful of processes can keep a steady
background of shoppers going. Its user classes mirror the shopper personas,
with the same mix:

    BrowseUser 40   categories, product lists by aisle and page, product detail
    SearchUser 25   searches (some find nothing), sorting, product detail
    BuyerUser  20   demo customer, adds 1-3 items; checks out on ~15% of visits
    LoyalUser   8   demo customer, order history, an order, reorder; checks out ~30% of the time
    AdminUser   2   low stock and a sales report (only if SHOPLITE_ADMIN_TOKEN is set)

Every request carries X-Synthetic-Source: locust/<type>. Expected outcomes
are not failures: a 409 (not enough stock) and an empty cart are marked as
successes in Locust's statistics. A 503 or 5xx is a real failure.

How many users run, and when, comes from shape.py (the daily curve). With
LOAD_SHAPE=off, set the numbers yourself (web UI, or -u/-r on the command line).

Run (see README.md):
    locust -f shoplite/traffic/load/locustfile.py --host http://localhost:8000
"""

import os
import random
from datetime import date, timedelta

from locust import HttpUser, between, task

# The load shape is used automatically when its class is importable from this
# file. LOAD_SHAPE=off leaves it out, for manual control in the web UI.
if os.environ.get("LOAD_SHAPE", "on").strip().lower() != "off":
    from shape import DailyCurveShape  # noqa: F401  (Locust finds it by its base class)

ADMIN_TOKEN = os.environ.get("SHOPLITE_ADMIN_TOKEN", "").strip()

SEARCH_TERMS = (
    "drill", "hammer", "screws", "wrench", "pliers", "paint", "primer", "faucet", "valve",
    "led bulb", "outlet", "extension cord", "garden hose", "shovel", "ladder", "tool box",
    "washers", "anchors", "sander", "battery", "shelving", "sprinkler", "nails", "bolts",
)
EMPTY_TERMS = ("refrigerator", "sofa", "drlll", "xqzt")      # find nothing, like real failed searches
SORTS = ("name", "price_asc", "price_desc", "newest")
PAYMENTS = (("card", 70), ("paypal", 20), ("gift_card", 10))

# How often a visit ends in an order. Real stores convert a few percent of
# visits; these keep Locust's share at roughly 35-80 orders on a light
# business day (on top of the browser shoppers'). Unconverted carts stay
# open, so a later visit checks out everything gathered so far.
BUYER_CHECKOUT_SHARE = 0.15
LOYAL_CHECKOUT_SHARE = 0.30


class ShopLiteUser(HttpUser):
    """Shared behaviour. Each simulated user pauses 2-8 s between tasks (a
    little slower than the browser shoppers, since one task here is a whole
    page's worth of API calls)."""

    abstract = True
    wait_time = between(2, 8)
    tag = "base"

    def on_start(self):
        self.client.headers["X-Synthetic-Source"] = f"locust/{self.tag}"
        self.category_ids: list[int] = []

    # --- Catalog helpers --------------------------------------------------------

    def categories(self) -> list[int]:
        if not self.category_ids:
            with self.client.get("/api/categories", name="/api/categories", catch_response=True) as r:
                if r.status_code == 200:
                    self.category_ids = [c["category_id"] for c in r.json()]
                    r.success()
                else:
                    r.failure(f"HTTP {r.status_code}")
        return self.category_ids

    def list_products(self, **params) -> list[dict]:
        """GET /api/products; returns the products on that page ([] on errors)."""
        with self.client.get("/api/products", params=params, name="/api/products",
                             catch_response=True) as r:
            if r.status_code == 200:
                r.success()
                return r.json()["items"]
            r.failure(f"HTTP {r.status_code}")
            return []

    def product_detail(self, product_id: int) -> dict | None:
        with self.client.get(f"/api/products/{product_id}", name="/api/products/[id]",
                             catch_response=True) as r:
            if r.status_code == 200:
                r.success()
                return r.json()
            if r.status_code == 404:                 # discontinued since it was listed: normal
                r.success()
                return None
            r.failure(f"HTTP {r.status_code}")
            return None


class BrowseUser(ShopLiteUser):
    weight = 40
    tag = "browse"

    @task
    def browse_an_aisle(self):
        ids = self.categories()
        params = {"page": random.choice((1, 1, 1, 2, 3))}
        if ids and random.random() < 0.8:
            params["category_id"] = random.choice(ids)
        products = self.list_products(**params)
        for product in random.sample(products, k=min(len(products), random.randint(1, 2))):
            self.product_detail(product["product_id"])


class SearchUser(ShopLiteUser):
    weight = 25
    tag = "search"

    @task
    def search(self):
        term = random.choice(EMPTY_TERMS) if random.random() < 0.1 else random.choice(SEARCH_TERMS)
        products = self.list_products(q=term)
        if products and random.random() < 0.4:
            products = self.list_products(q=term, sort=random.choice(SORTS))
        if products:
            self.product_detail(random.choice(products)["product_id"])


class CustomerUser(ShopLiteUser):
    """A signed-in demo customer (X-Customer-Id), picked at start. Customers
    pause longer between visits (5-15 s) than browsers do."""

    abstract = True
    wait_time = between(5, 15)

    def on_start(self):
        super().on_start()
        self.customer_id = None
        with self.client.get("/api/demo-account", name="/api/demo-account", catch_response=True) as r:
            if r.status_code == 200:
                self.customer_id = r.json()["customer_id"]
                self.client.headers["X-Customer-Id"] = str(self.customer_id)
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}")

    def add_to_cart(self, product_id: int, quantity: int) -> bool:
        with self.client.post("/api/cart/items", json={"product_id": product_id, "quantity": quantity},
                              name="/api/cart/items", catch_response=True) as r:
            if r.status_code in (200, 201):
                r.success()
                return True
            if r.status_code == 409:                 # not enough stock: a normal outcome
                r.success()
                return False
            r.failure(f"HTTP {r.status_code}")
            return False

    def checkout(self) -> None:
        method = random.choices([m for m, _ in PAYMENTS], weights=[w for _, w in PAYMENTS])[0]
        # No shipping in the body: the API ships to the customer's address on file.
        with self.client.post("/api/checkout", json={"payment_method": method},
                              name="/api/checkout", catch_response=True) as r:
            if r.status_code == 201:                 # paid or payment_failed: both normal
                r.success()
            elif r.status_code in (400, 409):        # empty cart, or stock ran out: normal
                r.success()
            else:
                r.failure(f"HTTP {r.status_code}")


class BuyerUser(CustomerUser):
    weight = 20
    tag = "buyer"

    @task
    def buy(self):
        if not self.customer_id:
            return
        added = 0
        for _ in range(random.randint(1, 3)):
            products = self.list_products(category_id=random.choice(self.categories() or [None]),
                                          page=random.choice((1, 1, 2)))
            in_stock = [p for p in products if p.get("in_stock")]
            if not in_stock:
                continue
            product = random.choice(in_stock)
            self.product_detail(product["product_id"])
            added += self.add_to_cart(product["product_id"], random.choices((1, 2, 3), weights=(80, 15, 5))[0])
        self.client.get("/api/cart", name="/api/cart")
        if added and random.random() < BUYER_CHECKOUT_SHARE:
            self.checkout()


class LoyalUser(CustomerUser):
    weight = 8
    tag = "loyal"

    @task
    def reorder(self):
        if not self.customer_id:
            return
        with self.client.get("/api/orders", params={"page": 1}, name="/api/orders", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"HTTP {r.status_code}")
                return
            r.success()
            orders = r.json()["items"]
        if not orders:
            return
        order_id = random.choice(orders)["order_id"]
        self.client.get(f"/api/orders/{order_id}", name="/api/orders/[id]")
        with self.client.post(f"/api/orders/{order_id}/reorder", name="/api/orders/[id]/reorder",
                              catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"HTTP {r.status_code}")
                return
            r.success()
            added = r.json().get("added", 0)
        if added and random.random() < LOYAL_CHECKOUT_SHARE:
            self.checkout()


class AdminUser(ShopLiteUser):
    weight = 2
    tag = "admin"
    # Without a token this class is left out entirely (abstract classes are
    # never started), instead of failing every request with 401.
    abstract = not ADMIN_TOKEN

    def on_start(self):
        super().on_start()
        self.client.headers["X-Admin-Token"] = ADMIN_TOKEN

    @task
    def check_stock_and_report(self):
        self.client.get("/api/admin/inventory", params={"low_stock_only": True},
                        name="/api/admin/inventory")
        days = random.randint(7, 90)
        end = date.today() - timedelta(days=random.randint(0, 365 - days))
        start = end - timedelta(days=days - 1)
        self.client.get("/api/admin/reports/sales",
                        params={"start_date": start.isoformat(), "end_date": end.isoformat(),
                                "group_by": random.choice(("day", "product", "state"))},
                        name="/api/admin/reports/sales")
