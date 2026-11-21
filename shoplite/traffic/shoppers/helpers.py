"""Shared helpers for the persona scripts.

  - Session: everything one shopping session needs (page, settings, random
    numbers, recorder, API watcher), passed to each persona.
  - think(): a human-like pause between actions.
  - Search terms, including typos and terms that match nothing.
  - synthetic_headers(): the X-Synthetic-Source tag every request carries.
  - Recorder / Outcome: what happened in a session (steps, result, timings).
  - ApiWatcher: counts the API's answers as the browser sees them, so a
    declined payment, a 409, or a 503 is recorded as a normal OUTCOME.
  - PageBroken: the one real failure, "the page itself is broken" (an
    expected element never appeared). Only this saves screenshots/traces.

Selectors: personas find elements ONLY by data-testid (page.get_by_test_id),
the storefront's stable contract (shoplite/web/README.md), so restyles don't
break the shoppers.
"""

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse

from playwright.async_api import Page, Response

from settings import Settings

SYNTHETIC_HEADER = "X-Synthetic-Source"

# Outcomes a session can end with. Everything except "error" is a normal,
# expected result to record, not a failure.
OUTCOMES = (
    "browsed",        # looked around and left (browser persona)
    "searched",       # searched, found results, left without buying
    "empty_search",   # search found nothing (typos and nonsense do this)
    "abandoned",      # added to cart, then left without checking out
    "paid",           # order placed and paid
    "declined",       # order placed, payment declined (about 1 in 50)
    "out_of_stock",   # 409: not enough stock when adding or at checkout
    "reordered",      # loyal customer reordered a past order and paid
    "nothing_to_reorder",  # loyal customer's past items couldn't be added
    "admin_done",     # admin checked stock and ran a report
    "unavailable",    # the API answered 503 (database down or overloaded)
    "error",          # the page itself was broken (PageBroken) or the script crashed
)


class PageBroken(Exception):
    """An element the storefront always shows didn't appear in time."""


def describe_failure(error: Exception, last_step: str) -> str:
    """Turn an exception into one readable sentence for the summary.

    PageBroken messages are already written for people. A raw Playwright
    timeout carries its "call log": the locator it was using and what it kept
    waiting for. This pulls out the action (e.g. click), the data-testid
    elements in the locator, and the last reason Playwright gave (e.g.
    "element is not enabled", "element is not stable", "<div ...> intercepts
    pointer events"), and adds the persona's last completed step.
    """
    after = f" (after step: {last_step})" if last_step else ""
    if isinstance(error, PageBroken):
        return f"page broken: {error}{after}"
    text = str(error).strip() or error.__class__.__name__
    lines = [line.strip().lstrip("-").strip() for line in text.splitlines()]
    first = lines[0] if lines else text
    if "Timeout" not in first:
        return f"{error.__class__.__name__}: {first[:300]}{after}"
    action = first.split(":", 1)[0].replace("Locator.", "").replace("Page.", "")
    test_ids = re.findall(r'get_by_test_id\("([^"]+)"\)|data-testid=\\?"([^"\\]+)', text)
    names = [a or b for a, b in test_ids]
    target = " inside ".join(reversed(names)) if names else "an element"
    reasons = [line for line in lines[1:]
               if line and not line.startswith(("waiting", "retrying", "attempting", "Call log",
                                                "locator resolved", "scrolling", "done scrolling"))]
    reason = reasons[-1] if reasons else "it never became ready"
    seconds = re.search(r"Timeout (\d+)ms", first)
    waited = f"{int(seconds.group(1)) // 1000}s" if seconds else "the timeout"
    return f"timed out after {waited} trying to {action} {target}: {reason[:200]}{after}"


# ---------------------------------------------------------------------------
# Search terms
# ---------------------------------------------------------------------------
# Real item words from the catalog (shoplite/db/seed.py), as a shopper would
# type them.
SEARCH_TERMS = (
    "drill", "cordless drill", "hammer", "claw hammer", "screwdriver set", "wrench", "pliers",
    "tape measure", "level", "hand saw", "impact driver", "circular saw", "sander", "jigsaw",
    "grinder", "battery", "screws", "drywall screws", "nails", "bolts", "washers", "anchors",
    "paint", "primer", "spray paint", "roller", "brush", "painter's tape", "faucet", "pex",
    "valve", "p-trap", "pipe wrench", "sump pump", "led bulb", "outlet", "light switch",
    "extension cord", "wire", "breaker", "smart plug", "work light", "garden hose", "shovel",
    "rake", "wheelbarrow", "sprinkler", "potting mix", "pruning shears", "leaf blower",
    "tool box", "shelving", "storage bin", "pegboard", "tool chest", "hooks", "ladder", "workbench",
)
# Things the store doesn't sell: these find nothing, like real failed searches.
NONSENSE_TERMS = ("refrigerator", "sofa", "laptop", "garden gnome", "xqzt", "bicycle")


def typo(term: str, rng: random.Random) -> str:
    """One realistic slip: a dropped, doubled, or swapped letter."""
    letters = [i for i, ch in enumerate(term) if ch.isalpha()]
    if len(letters) < 3:
        return term
    i = rng.choice(letters[1:-1])
    kind = rng.choice(("drop", "double", "swap"))
    if kind == "drop":
        return term[:i] + term[i + 1:]
    if kind == "double":
        return term[:i] + term[i] + term[i:]
    return term[:i] + term[i + 1] + term[i] + term[i + 2:]


def random_search_term(rng: random.Random) -> tuple[str, str]:
    """(term, kind): kind is "plain" (~75%), "typo" (~17%), or "nonsense" (~8%)."""
    roll = rng.random()
    if roll < 0.08:
        return rng.choice(NONSENSE_TERMS), "nonsense"
    term = rng.choice(SEARCH_TERMS)
    if roll < 0.25:
        return typo(term, rng), "typo"
    return term, "plain"


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------

def synthetic_headers(persona: str) -> dict[str, str]:
    """Sent with EVERY request the browser makes (pages and API calls), so the
    API logs e.g. "synthetic playwright/buyer". The API treats it like any
    other request; CORS allows the header from API 1.1.0 on."""
    return {SYNTHETIC_HEADER: f"playwright/{persona}"}


# ---------------------------------------------------------------------------
# Recording what happened
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    persona: str
    mobile: bool
    outcome: str = "error"
    steps: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    seconds: float = 0.0
    detail: str = ""
    api_status_counts: dict[str, int] = field(default_factory=dict)


class Recorder:
    """Collects the steps of one session and its final outcome."""

    def __init__(self, persona: str, mobile: bool):
        self.result = Outcome(persona=persona, mobile=mobile)
        self._clock = time.perf_counter()

    def step(self, description: str) -> None:
        self.result.steps.append(description)

    def finish(self, outcome: str, detail: str = "") -> Outcome:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        self.result.outcome = outcome
        self.result.detail = detail
        self.result.seconds = round(time.perf_counter() - self._clock, 1)
        return self.result


class ApiWatcher:
    """Counts the API's responses by status code, as the browser received them.

    Personas use it to tell a normal 409 (out of stock) or 503 (unavailable)
    apart from a broken page. Only responses from the API host are counted.
    """

    def __init__(self, page: Page, api_url: str):
        self._api_host = urlparse(api_url).netloc
        self.statuses: list[tuple[int, str, str]] = []      # (status, method, path)
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        parsed = urlparse(response.url)
        if parsed.netloc == self._api_host:
            self.statuses.append((response.status, response.request.method, parsed.path))

    def mark(self) -> int:
        """A position to look back from (see seen())."""
        return len(self.statuses)

    def seen(self, status: int, path_contains: str = "", since: int = 0) -> bool:
        """Has the API answered `status` (on a path containing path_contains)
        since the mark?"""
        return any(s == status and path_contains in p for s, _m, p in self.statuses[since:])

    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for status, _method, _path in self.statuses:
            totals[str(status)] = totals.get(str(status), 0) + 1
        return totals


# ---------------------------------------------------------------------------
# One session
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """Everything a persona needs. Personas are `async def run(session)`
    functions that return session.recorder.finish(...)."""
    page: Page
    settings: Settings
    rng: random.Random
    recorder: Recorder
    api: ApiWatcher

    async def think(self) -> None:
        """Pause like a person reading the page."""
        await asyncio.sleep(self.rng.uniform(self.settings.think_min, self.settings.think_max))

    def testid(self, test_id: str):
        return self.page.get_by_test_id(test_id)

    async def goto(self, path: str) -> None:
        """Open a storefront page (path like "/" or "/account")."""
        await self.page.goto(self.settings.storefront_url + path, wait_until="domcontentloaded")

    async def expect(self, test_id: str, timeout_ms: int | None = None):
        """Wait for the first element with this data-testid to be visible.
        Raises PageBroken if it never shows up: the page itself is broken."""
        locator = self.testid(test_id).first
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms or self.settings.step_timeout_ms)
        except Exception as error:
            raise PageBroken(f"'{test_id}' never appeared on {self.page.url}") from error
        return locator

    async def first_visible(self, *test_ids: str, timeout_ms: int | None = None) -> str:
        """Wait until ANY of these elements is visible; return which one.
        Used where the page can legitimately end in several states (results
        or empty state, success or error notice)."""
        timeout = (timeout_ms or self.settings.step_timeout_ms) / 1000
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for test_id in test_ids:
                if await self.testid(test_id).first.is_visible():
                    return test_id
            await asyncio.sleep(0.25)
        raise PageBroken(f"none of {', '.join(test_ids)} appeared on {self.page.url}")

    async def pick(self, test_id: str):
        """A random visible element among those with this data-testid."""
        await self.expect(test_id)
        items = self.testid(test_id)
        count = await items.count()
        return items.nth(self.rng.randrange(count))

    async def sign_in_as_demo_customer(self) -> None:
        """Sign in through the storefront's "Sign in as a demo customer"."""
        await self.goto("/signin")
        self.recorder.step("open sign-in")
        await self.think()
        await (await self.expect("signin-demo-button")).click()
        # The storefront returns to the catalog once signed in.
        await self.page.wait_for_url(lambda url: "/signin" not in url,
                                     timeout=self.settings.step_timeout_ms)
        await self.expect("profile-button")
        self.recorder.step("signed in as a demo customer")


# ---------------------------------------------------------------------------
# Catalog actions shared by several personas
# ---------------------------------------------------------------------------

async def wait_for_api(session: Session, method: str, path_contains: str, since: int,
                       timeout_ms: int | None = None) -> int:
    """Wait until the API has answered `method` on a path containing
    path_contains (since the mark); return its status code.

    A click that should reach the API but never does means the page is
    broken (button dead, JavaScript error), so that raises PageBroken.
    """
    deadline = time.monotonic() + (timeout_ms or session.settings.step_timeout_ms) / 1000
    while time.monotonic() < deadline:
        for status, seen_method, path in session.api.statuses[since:]:
            if seen_method == method and path_contains in path:
                return status
        await asyncio.sleep(0.2)
    raise PageBroken(f"no API answer to {method} {path_contains} after the click on {session.page.url}")


# After a catalog click, how long to wait before deciding the click changed
# nothing. Next.js updates the address bar almost at once, so 2 s is generous.
NO_CHANGE_GRACE_SECONDS = 2.0

# Query values the catalog uses when a parameter is absent (shoplite/web/app/page.js).
CATALOG_DEFAULTS = {("sort", "name"), ("page", "1")}


def catalog_view(url: str) -> tuple[str, frozenset]:
    """What a catalog URL MEANS: its path plus its decoded query values,
    ignoring order, spelling, and defaults.

    The storefront spells the same view in different ways: the header search
    writes "?q=circular%20saw" (encodeURIComponent), while the sort box and
    aisle pills write "?q=circular+saw" (URLSearchParams) and leave out
    defaults such as sort=name. The catalog only fetches when these decoded
    values change, so comparing raw URL strings would mistake a no-op for a
    navigation.
    """
    parsed = urlparse(url)
    values = {(key, value) for key, value in parse_qsl(parsed.query) if value}   # decodes %20 and +
    return parsed.path or "/", frozenset(values - CATALOG_DEFAULTS)


async def catalog_state(session: Session) -> str:
    """What the catalog shows right now: "results", "empty", or "unavailable"."""
    shown = await session.first_visible("product-tile", "empty-state", "notice-error")
    return {"product-tile": "results", "empty-state": "empty"}.get(shown, "unavailable")


async def wait_for_catalog(session: Session, since: int, url_before: str | None = None) -> str:
    """After a catalog navigation: wait for the product list to arrive, then
    for it to render. Returns "results", "empty", or "unavailable".

    url_before: the page URL just before the click (pass it for clicks; leave
    it out after page.goto). A click that leaves the catalog view unchanged,
    such as the aisle that's already selected or the same search or sort
    again, makes the storefront (correctly) load nothing new. So if, after a
    short grace period, no product request was made and the URL still means
    the same view (catalog_view: decoded, defaults ignored, so "%20" vs "+"
    or an added "sort=name" don't count as changes), the click was a no-op:
    report what's on screen. Only a click that changed the VIEW and then got
    no API answer means the page is broken.
    """
    started = time.monotonic()
    deadline = started + session.settings.step_timeout_ms / 1000
    while time.monotonic() < deadline:
        for status, method, path in session.api.statuses[since:]:
            if method == "GET" and path.startswith("/api/products"):
                if status == 503:
                    return "unavailable"
                return await catalog_state(session)
        if (url_before is not None
                and time.monotonic() - started >= NO_CHANGE_GRACE_SECONDS
                and catalog_view(session.page.url) == catalog_view(url_before)):
            session.recorder.step("(already showing that: nothing new to load)")
            return await catalog_state(session)
        await asyncio.sleep(0.2)
    raise PageBroken(f"the view changed from {url_before or 'a new page'} to {session.page.url} but the "
                     f"product list never loaded (no API answer to GET /api/products)")


async def back_to_catalog(session: Session) -> str:
    """Make sure the catalog (aisle pills, product cards) is on screen. From
    any other page, open the home page. Returns wait_for_catalog's result,
    or "results" if already there."""
    if urlparse(session.page.url).path == "/" and await session.testid("aisle-link").first.is_visible():
        return "results"
    mark = session.api.mark()
    await session.goto("/")
    session.recorder.step("went to the catalog")
    return await wait_for_catalog(session, mark)


async def search(session: Session, term: str) -> str:
    """Type a search in the header and submit it. Returns what wait_for_catalog returns."""
    box = await session.expect("header-search-input")
    await box.fill("")
    await box.type(term, delay=session.rng.randint(40, 140))      # typing speed of a person
    mark, url_before = session.api.mark(), session.page.url
    if session.rng.random() < 0.5:
        await (await session.expect("header-search-submit")).click()
    else:
        await box.press("Enter")
    result = await wait_for_catalog(session, mark, url_before)   # the same search again changes nothing
    session.recorder.step(f"searched {term!r}: {result}")
    return result


async def open_random_product(session: Session) -> str:
    """Click a random product card and wait for the product page. Returns the product name."""
    await (await session.pick("product-tile-link")).click()
    name = (await (await session.expect("product-name")).inner_text()).strip()
    session.recorder.step(f"opened product {name!r}")
    return name


async def add_this_product(session: Session, quantity: int = 1) -> str:
    """On a product page (signed in): set the quantity and add to cart.

    Returns "added", "out_of_stock" (409), "unavailable" (503), or "skipped"
    when the product can't be bought (out of stock: no Add button).
    """
    add_button = session.testid("add-to-cart").first
    if not await add_button.is_visible():
        session.recorder.step("product can't be added (out of stock)")
        return "skipped"
    if quantity > 1:
        await session.testid("quantity-input").first.fill(str(quantity))
    mark = session.api.mark()
    await add_button.click()
    status = await wait_for_api(session, "POST", "/api/cart/items", mark)
    await session.expect("add-to-cart-message")
    result = {409: "out_of_stock", 503: "unavailable"}.get(status, "added" if status < 300 else "error")
    if result == "error":
        raise PageBroken(f"adding to cart returned HTTP {status}")
    session.recorder.step(f"add to cart x{quantity}: {result}")
    return result


async def quick_add_from_card(session: Session) -> str:
    """Use a product card's "+ Add" button (signed in). Same return values as add_this_product.

    Every card shows the button when signed in, but on an out-of-stock card it
    is DISABLED and reads "Sold out". Like a shopper, choose only among cards
    whose button is enabled; clicking a disabled button would just wait for a
    state that never comes. (An enabled button that then gets no API answer
    still means the page is broken; see wait_for_api.)
    """
    await session.expect("product-tile")
    buttons = session.page.locator('[data-testid="tile-add-to-cart"]:enabled')
    count = await buttons.count()
    if count == 0:
        session.recorder.step("every card on this page is sold out")
        return "skipped"
    add_button = buttons.nth(session.rng.randrange(count))
    mark = session.api.mark()
    await add_button.click()
    status = await wait_for_api(session, "POST", "/api/cart/items", mark)
    result = {409: "out_of_stock", 503: "unavailable"}.get(status, "added" if status < 300 else "error")
    if result == "error":
        raise PageBroken(f"quick add returned HTTP {status}")
    session.recorder.step(f"quick add from a card: {result}")
    return result
