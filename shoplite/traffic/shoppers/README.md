# shoplite/traffic/shoppers

Simulated shoppers: real Chromium browsers (Playwright) that open the ShopLite storefront and click
through it like people. They follow the full path a real visit takes: page load, JavaScript, CORS,
the API, and SQL Server. That's the traffic Atlas watches.

> **Before running against production, deploy API 1.1.0.** Every request carries an
> `X-Synthetic-Source` header, and older APIs don't allow it in CORS, so the browser blocks every API
> call. Locally, run the 1.1.0 API from this repo.

## Personas

Each session picks one persona by weight. About 5% of sessions, of any persona, run on a phone-sized
screen.

| Persona | Share | What it does | Ends as |
|---|---|---|---|
| `browser` | 40 | Lands, opens aisles (sometimes via the hero buttons), pages through, opens products and "often bought together" items. Never signs in. | `browsed` |
| `searcher` | 25 | 1–3 searches with typos and things the store doesn't sell, retypes after empty results, sorts, opens products. 1 in 4 signs in, adds an item, and leaves. | `searched`, `empty_search`, `abandoned` |
| `buyer` | 20 | Signs in as a demo customer, adds 1–4 items (aisles, search, quick "+ Add"), checks out via the cart drawer or cart page, pays by card (presses **Use demo card**), PayPal, or gift card. | `paid`, `declined`, `out_of_stock` |
| `loyal` | 8 | Signs in, opens Orders, maybe filters to delivered, opens an order, Reorder, then checks out. | `reordered`, `declined`, `nothing_to_reorder` |
| `admin` | 2 | Signs in to admin with the token, inventory and low stock, then a sales report for a random 7–90 day range. | `admin_done` |

Any persona can also end as `unavailable` (the API answered 503) or `error` (the page itself was
broken). Only `error` is a failure: declines, 409s, empty searches, and 503s are normal results to
record.

How they work:
- **Selectors are `data-testid` only** (the contract in `shoplite/web/README.md`), so restyles don't
  break them.
- **Every action that should reach the API is confirmed by the API's own answer** (status code), not
  by reading text on the page.
- **Human-like timing:** 1–6 seconds of "think time" between actions, and typing at human speed.
- **Every request carries `X-Synthetic-Source: playwright/<persona>`.** The API logs it and otherwise
  treats the request like any other.

## Install (once)

From the repo root, with the root virtual environment active (Python 3.12):

```bash
pip install -r shoplite/traffic/shoppers/requirements.txt
python -m playwright install chromium
```

## Run locally

Start the local stack first: Docker databases, the API on <http://localhost:8000>, and the storefront
on <http://localhost:3000> (`shoplite/api/README.md`, `shoplite/web/README.md`).

```bash
cd shoplite/traffic/shoppers

# 1. Watch one persona shop, slowly, in a visible browser (your installed Google Chrome)
SHOPPERS_BROWSER_CHANNEL=chrome python run.py --headed --slow-mo 300 --persona buyer --duration 60 --concurrency 1

# 2. The normal mix, headless, for a few minutes
python run.py --duration 180
```

The defaults point at localhost, so nothing reaches production unless you set the URLs.

**Visible (headed) runs on a Mac: use your installed Chrome.** Playwright's bundled Chromium can crash
at launch in headed mode on macOS (`signal 10`, `BUS_ADRALN`), while headless runs work fine. Setting
`SHOPPERS_BROWSER_CHANNEL=chrome` makes Playwright drive the Google Chrome installed in
`/Applications` instead; it needs no extra download. Headless runs, and GitHub Actions, keep the
default bundled Chromium, so leave the setting empty there.

## Run against production

Normally GitHub Actions does this (`.github/workflows/shoppers.yml`). To run it from your Mac during a
VM session:

```bash
cd shoplite/traffic/shoppers
STOREFRONT_URL=https://shoplite-aadarsh.vercel.app \
API_URL=https://shoplite-aadarsh.northcentralus.cloudapp.azure.com \
python run.py --duration 120
```

The admin persona only runs when `SHOPLITE_ADMIN_TOKEN` is set. Set it in your shell from your
password manager; never write it into a file in the repo.

## Settings

All optional; environment variables, with the command line overriding some.

| Variable | Default | Meaning |
|---|---|---|
| `STOREFRONT_URL` | `http://localhost:3000` | Where the browsers go |
| `API_URL` | `http://localhost:8000` | The API (health check; counting its responses) |
| `SHOPLITE_ADMIN_TOKEN` | empty | Admin persona's token; empty means no admin sessions |
| `SHOPPERS_CONCURRENCY` | `2` (max 8) | Shoppers at the same time (`--concurrency`) |
| `SHOPPERS_DURATION_SECONDS` | `300` | Keep starting sessions this long (`--duration`); running sessions get 3 more minutes to finish |
| `SHOPPERS_THINK_MIN_SECONDS` / `_MAX_` | `1` / `6` | Pause between actions |
| `SHOPPERS_WEIGHTS` | `browser=40,searcher=25,buyer=20,loyal=8,admin=2` | Persona mix; unknown names are an error |
| `SHOPPERS_MOBILE_SHARE` | `0.05` | Share of sessions on a phone-sized screen |
| `SHOPPERS_HEADLESS` | `true` | `false` shows the windows (`--headed`) |
| `SHOPPERS_BROWSER_CHANNEL` | empty | Empty = Playwright's bundled Chromium (headless, CI). `chrome` = your installed Google Chrome, for local headed runs (`chrome-beta` and `msedge` also accepted). |
| `SHOPPERS_SLOW_MO_MS` | `0` | Slow every action (`--slow-mo`) |
| `SHOPPERS_STEP_TIMEOUT_MS` | `15000` | How long to wait for an element before calling the page broken |
| `SHOPPERS_ARTIFACTS_DIR` | `shoppers-artifacts` | Parent folder (ignored by Git); each run writes to its own `run-<UTC time>/` inside it |
| `SHOPPERS_SEED` | random | Repeat the same random choices |

Other options: `--persona <name>` runs only that persona; `--skip-health-check` skips the `/health`
check.

## Reading a run

**Console:** one line per session as it ends, e.g.
`[012] buyer (mobile): paid in 48.3s - by card`, then a summary.

**Summary** (also on the GitHub run page as the job summary):
- an outcome table (how many `paid`, `declined`, `browsed`, …),
- a persona table with average session length,
- the API status codes the browsers saw (`200 x412, 201 x6, 409 x1`),
- any broken pages, with the last step each one reached.

**One folder per run:** every run writes to its own folder, e.g.
`shoppers-artifacts/run-20261121-174405Z/` (the start time, UTC). The last line of the console output
names it. Evidence from different runs never mixes, so a screenshot always belongs to the summary
next to it. Delete old run folders whenever you like.

**`results.json`** (in the run's folder): every session's persona, device, outcome, detail, steps
(plain sentences), duration, and status counts. It contains no secrets.

**Reading an `error`:** the detail is one sentence saying what failed and where, e.g.

```
timed out after 15s trying to click tile-add-to-cart inside product-tile: element is not enabled (after step: opened an aisle)
```

That's the action, the `data-testid` element (and the element it was inside), Playwright's last reason
for waiting (`element is not enabled`, `element is not stable` while animating,
`<div …> intercepts pointer events` when something covers it), and the persona's last completed step.

**Exit code:** `0` when no page broke, or when the run was skipped because `/health` wasn't ok (the VM
is off, for example); `1` when at least one page broke; `2` for a settings error.

**Matching it on the server:** shopper requests show up in the API log as
`(customer 4821, synthetic playwright/buyer)`, and their orders are ordinary orders in SalesDB.

## When a page breaks

The session saves, in the run's folder:
- `NNN-<persona>[-mobile].png`, a full-page screenshot;
- `NNN-<persona>[-mobile]-trace.zip`, a Playwright trace: every action with its call log, every
  network request, and a snapshot of the page before and after each step.

Successful sessions keep nothing.

### Viewing a trace

**Use https://trace.playwright.dev in regular Google Chrome**, and drag the `-trace.zip` file onto the
page. It's Playwright's own trace viewer running in your browser; the file is read locally and isn't
uploaded anywhere.

`python -m playwright show-trace <file>` opens the same viewer in Playwright's bundled Chromium, in a
window, so on a Mac where the bundled Chromium crashes in headed mode (`signal 10`, `BUS_ADRALN`; see
`SHOPPERS_BROWSER_CHANNEL`), it crashes too. The website does the same job without it.

In the viewer, select the failed action (marked red) and open its **Log** tab: lines such as
`element is not enabled` say exactly why Playwright kept waiting.

## Secrets

- **The admin persona never records a trace.** Traces capture typed text and request headers, which
  would include the admin token, and artifacts in a public repo can be downloaded by any signed-in
  GitHub user. Before an admin screenshot, every password box is blanked.
- The settings object never prints the token, and no script logs it.
- Card details are the demo values from **Use demo card** and never leave the browser; the API only
  receives the payment method.

## Layout

```
shoplite/traffic/shoppers/
├── requirements.txt    Playwright
├── settings.py         every setting from environment variables, validated
├── helpers.py          Session, think time, search terms and typos, synthetic header,
│                       outcome recorder, API response watcher, shared catalog actions
├── personas/
│   ├── __init__.py     the persona list, and which ones never record traces
│   ├── browser.py
│   ├── searcher.py
│   ├── buyer.py        also the shared checkout step (place_order)
│   ├── loyal.py
│   └── admin.py
└── run.py              health check, concurrent sessions, artifacts, summary
```
