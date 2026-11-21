# shoplite/api

The **ShopLite API**: a Python 3.12 FastAPI backend that the storefront calls directly from the
browser. It reads and writes SalesDB through SQLAlchemy Core and pyodbc, using plain, readable SQL so
you (and later Atlas) can see exactly what reaches SQL Server. Built in Phase 1; deployed to the
Azure VM as a Windows service in Phase 2.

> **Demo sign-in, not real authentication.** Customers "sign in" by email (or as a random demo
> customer), and the storefront then sends their ID in the `X-Customer-Id` header. Admin pages send
> a shared token in `X-Admin-Token`. Anyone can forge either. That's acceptable for a demo store
> with simulated shoppers, and it's labeled as such in the code. Never reuse this for real users.

## Running locally

Prerequisites: the Phase 0 containers are healthy, and SalesDB has been built and seeded
(`shoplite/db/README.md`). Commands run from the repo root with the root `.venv` active.

1. Create the API's settings file and fill in the values:

   ```bash
   cp shoplite/api/.env.example shoplite/api/.env
   ```

   Use the `shoplite_app` password (`SHOPLITE_APP_PASSWORD` from the root `.env`), choose an admin
   token, and keep `http://localhost:3000` as the allowed origin.

2. Install the requirements:

   ```bash
   pip install -r shoplite/api/requirements.txt
   ```

3. Start the API:

   ```bash
   cd shoplite/api
   uvicorn app.main:app --reload --port 8000 --no-access-log
   ```

   `--reload` restarts the server when code changes (development only). `--no-access-log` turns
   off Uvicorn's own per-request lines, because the API logs each request itself, with its request
   ID, duration, and customer.

4. Open the interactive docs at <http://localhost:8000/docs>.

## Headers

| Header | Direction | Purpose |
|---|---|---|
| `X-Customer-Id` | request | The signed-in demo customer (cart, checkout, orders) |
| `X-Admin-Token` | request | Admin endpoints; must match `ADMIN_TOKEN` |
| `X-Request-ID` | both | Correlates one request across logs; generated if the caller doesn't send one |
| `X-App-Version` | response | The API version that handled the request (`APP_VERSION`) |

The API also tags its SQL Server sessions with the program name `ShopLite-API/<version>`, so they
can be found in `sys.dm_exec_sessions`.

## Endpoints

| Area | Endpoint | Behavior |
|---|---|---|
| Health | `GET /health` | Checks the database connection; returns status and version |
| Catalog | `GET /api/categories` | List categories |
| | `GET /api/products` | Search, filter by category, sort (name, price up/down, newest), paginate |
| | `GET /api/products/{id}` | Detail with stock, plus "often bought together" from order history |
| Account | `POST /api/login` | Demo sign-in by email |
| | `GET /api/demo-account` | Returns a random seeded customer |
| Cart | `GET /api/cart` | The customer's open cart |
| | `POST /api/cart/items` | Add or increase an item |
| | `PATCH /api/cart/items/{product_id}` | Change quantity |
| | `DELETE /api/cart/items/{product_id}` | Remove an item |
| Checkout | `POST /api/checkout` | Place an order from the cart and take payment |
| Orders | `GET /api/orders` | The customer's order history |
| | `GET /api/orders/{id}` | One order with its items |
| | `POST /api/orders/{id}/reorder` | Put a past order's items back in the cart |
| Admin | `GET /api/admin/inventory` | Inventory with a low-stock filter |
| | `GET /api/admin/reports/sales` | Sales by day, product, or state over a date range |
| | `GET /api/admin/reports/top-products` | Best sellers over a date range |

### Checkout in three steps

1. One short transaction locks the cart's inventory rows, checks stock, creates the order and its
   items, reduces stock, and marks the cart converted. Not enough stock: **409**, nothing changes.
2. After that commits, the simulated payment provider is called (short random delay, small chance
   of decline).
3. A second short transaction records the payment and sets the order status. A declined payment
   restores the stock.

The payment call is deliberately outside the transaction, so no locks are held while waiting.

## Errors

Errors return JSON `{"detail": "..."}` with the request ID in the `X-Request-ID` header.

| Status | Meaning |
|---|---|
| 400 | Invalid input, including report parameters rejected by the stored procedures |
| 401 | Missing or invalid `X-Customer-Id` or `X-Admin-Token` |
| 404 | The product, order, or customer doesn't exist (or isn't yours) |
| 409 | Not enough stock (adding to cart or at checkout) |
| 422 | Request body or query parameters failed validation (FastAPI's standard response) |
| 503 | The database is unreachable or a query timed out (for example, it was blocked) |
| 500 | Anything unexpected; look up the request ID in the API log |

## Tests

Smoke tests run against the local SalesDB (seeded) and change data like a real shopper would:

```bash
cd shoplite/api
python -m pytest
```

## Layout

```
shoplite/api/
├── .env.example         settings template (copy to .env)
├── requirements.txt
├── app/
│   ├── main.py          creates the app: CORS, middleware, routers
│   ├── config.py        settings from the environment, validated
│   ├── db.py            engine, connection pool, timeouts, session tag, SQL error helpers
│   ├── flags.py         feature flags (all off in Phase 1)
│   ├── middleware.py    request IDs, version header, request logging
│   ├── schemas.py       request and response models
│   └── routers/         one file per area: health, catalog, account, cart, checkout, orders, admin
└── tests/               smoke tests (pytest)
```
