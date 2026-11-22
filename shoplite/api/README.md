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

1. Settings: **nothing to copy.** The API reads the root `.env` (layered settings,
   `tools/envconfig.py`): `SHOPLITE_APP_PASSWORD` is its database password (`DB_PASSWORD`),
   `ADMIN_TOKEN` its admin token, and `SQL_TRUST_SERVER_CERTIFICATE=true` trusts the local
   container's certificate. Everything else has a local default (`http://localhost:3000` as the
   allowed origin). Create `shoplite/api/.env` from its `.env.example` only to override one value.

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
   ID, duration, customer, and synthetic tag:

   ```
   2026-11-21 14:03:22,123 INFO    shoplite.request [9f2c...] GET /api/products?q=drill -> 200 in 41 ms (customer -, synthetic playwright/searcher)
   ```

4. Open the interactive docs at <http://localhost:8000/docs>.

## Headers

| Header | Direction | Purpose |
|---|---|---|
| `X-Customer-Id` | request | The signed-in demo customer (cart, checkout, orders) |
| `X-Admin-Token` | request | Admin endpoints; must match `ADMIN_TOKEN` |
| `X-Request-ID` | both | Correlates one request across logs; generated if the caller doesn't send one |
| `X-App-Version` | response | The API version that handled the request (`APP_VERSION`) |
| `X-Synthetic-Source` | request | Marks simulated traffic, e.g. `playwright/buyer`, `locust/browse` (Phase 3). Logged with the request and **otherwise ignored**: tagged and untagged requests behave the same. Letters, digits, and `. _ - /`, at most 64 characters; anything else is logged as `-` (untagged), never rejected. Allowed by CORS, so the storefront's browser shoppers can send it. |

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
| | `POST /api/admin/restock` | "Receive a shipment": tops up products at or below their reorder level to `target_quantity` (default 100, 1–1,000), lowest stock first, at most `max_products` per call (default 500, 1–2,000). One short transaction. Returns `products_restocked`, `units_added`, `target_quantity`, `still_low`. An empty body uses the defaults. Called nightly by `.github/workflows/restock.yml`. |

### What's new in 1.1.0 (Phase 3)

- `X-Synthetic-Source` is accepted (CORS) and written into every request's log line.
- `POST /api/admin/restock` keeps simulated shopping from emptying the shelves.
- Deploy with `deploy.ps1 -Version 1.1.0`; no database changes.

### What's new in 1.2.0 (Phase 5)

- **Application Insights** (`app/telemetry.py`), through the Azure Monitor OpenTelemetry distro: every
  request (`AppRequests`), every SQL call as a dependency with its parameterized text (`AppDependencies`),
  and errors (`AppExceptions`), linked by `OperationId`. Custom dimensions on each request:
  `shoplite.synthetic_source` (the `X-Synthetic-Source` tag) and `shoplite.request_id`. **Off unless
  `APPLICATIONINSIGHTS_CONNECTION_STRING` is set**, so local runs and tests send nothing.
- **Sampling:** `TELEMETRY_SAMPLING_RATIO` (default 0.25) of requests are kept, each with all its SQL
  calls; `ItemCount` on each row says how many requests it stands for, so count with
  `sum(ItemCount)` (the alert rules do). The API's log lines aren't exported (they're already in files).
- **Feature flag `search_ranking_v2`** (off by default): searches with the default sort come back in
  relevance order (names starting with the search term first), scored by a **simulated ranking
  service** (`app/ranking.py`) that takes `RANKING_SERVICE_DELAY_MS` (2500) and fails
  `RANKING_SERVICE_ERROR_RATE` (0.5) of the time with `RankingServiceError` (a 500). It's the Phase 5
  "API fault" incident: a buggy feature rollout, switched on and off by the incident injector. In
  Application Insights the ranking call is a dependency (`POST /v2/rank` to `ranking-service`).
- **New packages:** `pip install -r requirements.txt` before starting 1.2.0 (locally too); `deploy.ps1`
  installs them on the VM.
- **New settings** (`.env.example`, `deploy/.env.production.example`):
  `APPLICATIONINSIGHTS_CONNECTION_STRING`, `TELEMETRY_SAMPLING_RATIO`, `RANKING_SERVICE_DELAY_MS`,
  `RANKING_SERVICE_ERROR_RATE`. On the VM, **add them to the existing `shoplite\api\.env`** (don't
  replace the file: it holds the production passwords), then deploy with `deploy.ps1 -Version 1.2.0`.
  No database changes.

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

Smoke tests run against the local SalesDB (seeded) and change data like a real shopper would. The
restock test restocks exactly one low-stock product per run. **Never run them against production.**
`test_telemetry_and_ranking.py` covers 1.2.0: telemetry stays off in tests (the conftest forces an
empty connection string), the custom dimensions and exception recording on a real OpenTelemetry span
(kept in memory), and the ranking flag (order, errors, delay, and the search endpoint's 500).

```bash
cd shoplite/api
python -m pytest
```

## Layout

```
shoplite/api/
├── .env.example         optional local overrides (production: deploy/.env.production.example)
├── requirements.txt
├── app/
│   ├── main.py          creates the app: CORS, middleware, routers
│   ├── config.py        settings from the environment, validated
│   ├── db.py            engine, connection pool, timeouts, session tag, SQL error helpers
│   ├── flags.py         feature flags (search_ranking_v2; all off by default)
│   ├── middleware.py    request IDs, version header, request logging, telemetry tags
│   ├── ranking.py       search ranking v2 and its simulated ranking service (Phase 5)
│   ├── telemetry.py     Application Insights via OpenTelemetry (Phase 5)
│   ├── schemas.py       request and response models
│   └── routers/         one file per area: health, catalog, account, cart, checkout, orders, admin
└── tests/               smoke tests (pytest)
```
