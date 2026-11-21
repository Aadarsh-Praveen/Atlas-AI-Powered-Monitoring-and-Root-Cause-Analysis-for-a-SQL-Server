"""Browser persona (~40%): looks around and leaves, never signs in.

Lands on the home page, then 2-5 of: open an aisle (sometimes through the
hero's buttons), go to the next page, open a product (and sometimes one of its
"often bought together" products), go back. Ends "browsed".

Generates the read-only catalog traffic most real visitors produce:
GET /api/categories, /api/products (filters, pages), /api/products/{id}.
"""

from helpers import Outcome, Session, open_random_product, wait_for_catalog

HERO_BUTTONS = ("hero-cta-fasteners", "hero-cta-power-tools")


async def open_aisle(session: Session) -> None:
    """Open an aisle: a hero button (about 1 in 5) or an aisle pill. Re-opening
    the aisle already shown is fine: wait_for_catalog treats it as a no-op."""
    mark, url_before = session.api.mark(), session.page.url
    if session.rng.random() < 0.2 and await session.testid(HERO_BUTTONS[0]).first.is_visible():
        button = session.rng.choice(HERO_BUTTONS)
        await session.testid(button).first.click()
        session.recorder.step(f"clicked the hero's {button.removeprefix('hero-cta-')} button")
    else:
        pill = await session.pick("aisle-link")
        slug = await pill.get_attribute("data-slug") or "all"
        await pill.click()
        session.recorder.step(f"opened aisle {slug}")
    await wait_for_catalog(session, mark, url_before)


async def next_page(session: Session) -> bool:
    """Go to the next page if there is one (the disabled control has aria-disabled)."""
    control = session.testid("pagination-next").first
    if not await control.is_visible() or await control.get_attribute("aria-disabled") == "true":
        return False
    mark, url_before = session.api.mark(), session.page.url
    await control.click()
    await wait_for_catalog(session, mark, url_before)
    session.recorder.step("went to the next page")
    return True


async def look_at_a_product(session: Session) -> None:
    await open_random_product(session)
    pages_deep = 1
    await session.think()
    # Sometimes follow an "often bought together" suggestion.
    together = session.testid("bought-together").get_by_test_id("product-tile-link")
    if await together.count() and session.rng.random() < 0.3:
        await together.nth(session.rng.randrange(await together.count())).click()
        await session.expect("product-name")
        pages_deep += 1
        session.recorder.step("followed an 'often bought together' product")
        await session.think()
    for _ in range(pages_deep):                 # back to the catalog, page by page
        await session.page.go_back()
    await session.expect("aisle-link")
    session.recorder.step("went back to the catalog")


async def run(session: Session) -> Outcome:
    mark = session.api.mark()
    await session.goto("/")
    session.recorder.step("landed on the home page")
    await wait_for_catalog(session, mark)
    await session.think()
    await session.page.mouse.wheel(0, session.rng.randint(300, 1200))      # reading down the page

    for _ in range(session.rng.randint(2, 5)):
        roll = session.rng.random()
        if roll < 0.35:
            await open_aisle(session)
        elif roll < 0.5:
            if not await next_page(session):
                await open_aisle(session)
        else:
            await look_at_a_product(session)
        await session.think()

    return session.recorder.finish("browsed")
