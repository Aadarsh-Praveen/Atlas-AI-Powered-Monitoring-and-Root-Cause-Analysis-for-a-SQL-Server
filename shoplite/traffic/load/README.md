# shoplite/traffic/load

Background load with [Locust](https://locust.io): simulated users that call the ShopLite **API**
directly (no browser), following a daily curve with a Friday flash sale. The Playwright shoppers
(`../shoppers/`) are few but realistic; Locust adds the steady background a real store has.

> **Locust never runs on the VM.** Run it from your Mac or GitHub Actions, so Atlas sees the
> application's load, not the load generator's CPU and memory.

## User classes

They mirror the shopper personas, with the same mix. Every request carries
`X-Synthetic-Source: locust/<type>`.

| Class | Weight | Each visit |
|---|---|---|
| `BrowseUser` | 40 | Lists an aisle (or all products), usually page 1, opens 1–2 products |
| `SearchUser` | 25 | Searches (10% find nothing), sometimes sorts, opens a product |
| `BuyerUser` | 20 | As a random demo customer: adds 1–3 in-stock items, views the cart; checks out on ~15% of visits (the cart carries over) |
| `LoyalUser` | 8 | As a demo customer: order history, an order, reorder; checks out ~30% of the time |
| `AdminUser` | 2 | Low stock, then a sales report for a random 7–90 day range. **Only when `SHOPLITE_ADMIN_TOKEN` is set**; otherwise left out. |

Browsing users pause 2–8 seconds between visits; customers pause 5–15 seconds.

**Counted as successes (normal outcomes):** a 409 when adding to cart or at checkout (not enough
stock), a 400 at checkout (empty cart), a declined payment (still `201`), and a 404 for a product
that was discontinued after being listed. **Counted as failures:** 503s and any other unexpected
status. So Locust's failure rate is a real health signal.

## The load shape (`shape.py`)

How many users run follows ShopLite's business day in **Boston time** (daylight saving included):
about 10% of the peak overnight, rising to a **midday peak** (12:00), a dip mid-afternoon, an
**evening peak** (18:00), then quiet. On **Fridays 12:00–12:20** a **flash sale** multiplies the
users by `LOAD_FLASH_MULTIPLIER`.

| Variable | Default | Meaning |
|---|---|---|
| `LOAD_PEAK_USERS` | `5` | Users at the busiest normal moment ("light": about 1–3 requests a second) |
| `LOAD_FLASH_MULTIPLIER` | `2.0` | Friday flash-sale multiplier |
| `LOAD_DURATION_SECONDS` | `600` | Each session stops after this long |
| `LOAD_CLOCK` | unset | Pretend it's this Boston time, e.g. `fri 11:58`; the fake clock runs forward with the session |
| `LOAD_SHAPE` | `on` | `off` = no shape; you set users and spawn rate yourself |
| `SHOPLITE_ADMIN_TOKEN` | unset | Enables `AdminUser` |

**Safe defaults, on purpose.** The VM is a burstable B2as_v2: sustained load drains its CPU credits,
and then it's throttled to about 40% of its CPU. Five users at peak stays well under that, and the
flash sale lasts 20 minutes. Raise `LOAD_PEAK_USERS` slowly, watching **CPU Credits Remaining** on
the VM's Metrics page. Running out of credits on purpose is a Phase 5 incident, not an accident.

## Install (once)

From the repo root, with the root virtual environment active:

```bash
pip install -r shoplite/traffic/load/requirements.txt
```

## Run locally

Start the local stack first (Docker, then the API on <http://localhost:8000>).

**With the web UI** (charts of users, requests per second, and response times), controlling users
yourself:

```bash
LOAD_SHAPE=off locust -f shoplite/traffic/load/locustfile.py --host http://localhost:8000
```

Open <http://localhost:8089>, enter for example 5 users and a spawn rate of 1, and press **Start**.
Stop it from the page when you've seen enough.

**Headless, following the shape** (as GitHub Actions runs it), for two minutes, with an HTML report:

```bash
LOAD_DURATION_SECONDS=120 locust -f shoplite/traffic/load/locustfile.py \
  --host http://localhost:8000 --headless --html locust-report.html
```

**Test the Friday flash sale** without waiting for Friday: start the fake clock just before noon and
watch the user count double at "12:00":

```bash
LOAD_CLOCK="fri 11:58" LOAD_DURATION_SECONDS=300 locust -f shoplite/traffic/load/locustfile.py \
  --host http://localhost:8000 --headless --html locust-report.html
```

The shape only sets how many users run. Each user's own behavior (pauses, checkout share) stays the
same.

## Run against production

Normally `.github/workflows/load.yml` does this, on a schedule during business hours. From your Mac,
during a VM session:

```bash
LOAD_DURATION_SECONDS=300 locust -f shoplite/traffic/load/locustfile.py \
  --host https://shoplite-aadarsh.northcentralus.cloudapp.azure.com --headless --html locust-report.html
```

API 1.1.0 must be deployed (Locust doesn't need CORS, but the restock endpoint and synthetic-tag
logging come with 1.1.0). Watch **CPU Credits Remaining** while it runs.

## Reading the report

`locust-report.html` (open it in any browser) has:
- **Statistics**: one row per endpoint (`/api/products`, `/api/products/[id]`, `/api/checkout`, …)
  with request count, failures, and response-time percentiles. The median and 95th percentile are the
  numbers to compare from day to day.
- **Charts**: users over time (the shape), requests per second, and response times.
- **Failures**: each failing request with its status. Only 503s and unexpected errors land here.

On the server, Locust's requests appear in the API log as `synthetic locust/browse`,
`synthetic locust/buyer`, and so on; its orders are ordinary orders in SalesDB.

Report files (`locust-report*.html`) are ignored by Git; in GitHub Actions they're saved as workflow
artifacts.

## Layout

```
shoplite/traffic/load/
├── requirements.txt   Locust
├── locustfile.py      the five user classes; imports the shape unless LOAD_SHAPE=off
├── shape.py           the daily curve and the Friday flash sale (Boston time)
└── README.md
```
