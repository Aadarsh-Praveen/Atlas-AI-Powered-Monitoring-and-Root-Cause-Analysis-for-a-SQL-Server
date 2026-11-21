"""Searcher persona (~25%): searches, sometimes with typos, and rarely buys.

1-3 searches (plain words, typos, and things the store doesn't sell). After an
empty result, often retypes the plain word, as people do. With results: may
sort, opens one or two products. About 1 in 4 sessions signs in, adds an item,
and leaves without checking out (an abandoned cart).

Ends "abandoned" (added and left), "empty_search" (never found anything), or
"searched".
"""

from helpers import (SEARCH_TERMS, Outcome, Session, add_this_product, open_random_product,
                     random_search_term, search, wait_for_catalog)

SORT_VALUES = ("name", "price_asc", "price_desc", "newest")


async def run(session: Session) -> Outcome:
    rng = session.rng
    will_add = rng.random() < 0.25
    if will_add:
        await session.sign_in_as_demo_customer()   # needed to add to cart
    else:
        await session.goto("/")
        session.recorder.step("landed on the home page")
    await session.think()

    found_anything = False
    for _ in range(rng.randint(1, 3)):
        term, kind = random_search_term(rng)
        result = await search(session, term)
        if result == "unavailable":
            return session.recorder.finish("unavailable", "the product search returned an error")
        if result == "empty" and kind != "nonsense" and rng.random() < 0.6:
            await session.think()
            result = await search(session, rng.choice(SEARCH_TERMS))     # retyped properly
        if result != "results":
            await session.think()
            continue

        found_anything = True
        await session.think()
        if rng.random() < 0.4:
            value = rng.choice(SORT_VALUES)
            mark, url_before = session.api.mark(), session.page.url
            await (await session.expect("sort-select")).select_option(value)
            await wait_for_catalog(session, mark, url_before)      # the current sort changes nothing
            session.recorder.step(f"sorted by {value}")
            await session.think()

        for _ in range(rng.randint(1, 2)):
            await open_random_product(session)
            await session.think()
            if will_add:
                added = await add_this_product(session)
                if added in ("added", "out_of_stock", "unavailable"):
                    # Leave now, with the item (or the attempt) behind.
                    outcome = {"added": "abandoned"}.get(added, added)
                    return session.recorder.finish(outcome, "left without checking out")
            await session.page.go_back()
            await session.think()

    return session.recorder.finish("searched" if found_anything else "empty_search")
