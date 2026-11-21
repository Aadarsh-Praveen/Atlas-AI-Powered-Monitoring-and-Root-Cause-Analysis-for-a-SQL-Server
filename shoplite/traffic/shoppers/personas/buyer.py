"""Buyer persona (~20%): signs in, adds 1-4 items, checks out.

Finds items through a mix of aisles, search, and the cards' quick "+ Add".
Reaches checkout through the cart drawer or the full cart page. Pays by card
(70%), PayPal (20%), or gift card (10%); for card it presses "Use demo card"
(the checkout requires the card fields when paying by card). The card details
never leave the browser: the API only receives the payment method.

Ends "paid", "declined" (the simulated provider declines about 1 in 50),
"out_of_stock" (409 when adding or at checkout), or "unavailable" (503).
"""

from helpers import (SEARCH_TERMS, Outcome, PageBroken, Session, add_this_product, back_to_catalog,
                     open_random_product, quick_add_from_card, search, wait_for_api, wait_for_catalog)

PAYMENT_METHODS = (("card", 70), ("paypal", 20), ("gift_card", 10))


async def find_and_add_one(session: Session) -> str:
    """Find one product and add it. Returns add_this_product's result."""
    rng = session.rng
    roll = rng.random()
    if roll < 0.35:
        result = await search(session, rng.choice(SEARCH_TERMS))      # the header search is on every page
    else:
        # Aisle pills are only on the catalog page.
        if await back_to_catalog(session) == "unavailable":
            return "unavailable"
        mark, url_before = session.api.mark(), session.page.url
        await (await session.pick("aisle-link")).click()
        result = await wait_for_catalog(session, mark, url_before)   # may be the current aisle
        session.recorder.step("opened an aisle")
    if result == "unavailable":
        return "unavailable"
    if result == "empty":
        return "skipped"
    await session.think()

    if rng.random() < 0.25:
        return await quick_add_from_card(session)
    await open_random_product(session)
    await session.think()
    quantity = rng.choices((1, 2, 3), weights=(80, 15, 5))[0]
    return await add_this_product(session, quantity)


async def go_to_checkout(session: Session) -> None:
    """Through the cart drawer (quick view) or the full cart page."""
    await (await session.expect("nav-cart-link")).click()       # a plain click opens the drawer
    await session.expect("cart-drawer")
    session.recorder.step("opened the cart drawer")
    await session.think()
    if session.rng.random() < 0.5:
        await (await session.expect("cart-drawer-checkout")).click()
    else:
        await (await session.expect("cart-drawer-view-cart")).click()
        await session.expect("cart-item")
        session.recorder.step("opened the full cart")
        await session.think()
        await (await session.expect("checkout-button")).click()
    await session.expect("place-order-button")
    session.recorder.step("reached checkout")


async def place_order(session: Session) -> Outcome:
    """Choose a payment method, place the order, and record the result."""
    rng = session.rng
    method = rng.choices([m for m, _ in PAYMENT_METHODS], weights=[w for _, w in PAYMENT_METHODS])[0]
    await (await session.expect("payment-method-select")).select_option(method)
    if method == "card":
        await (await session.expect("use-demo-card")).click()
    session.recorder.step(f"paying by {method}")
    await session.think()

    mark = session.api.mark()
    await (await session.expect("place-order-button")).click()
    # The payment provider takes 0.2-0.8 s after the order is saved, so allow
    # longer than a normal step.
    status = await wait_for_api(session, "POST", "/api/checkout", mark,
                                timeout_ms=session.settings.step_timeout_ms * 2)
    if status == 409:
        return session.recorder.finish("out_of_stock", "stock ran out at checkout")
    if status == 503:
        return session.recorder.finish("unavailable", "checkout returned 503")
    if status >= 300:
        raise PageBroken(f"checkout returned HTTP {status}")

    result = await session.expect("order-result")
    order_status = await result.get_attribute("data-status")
    session.recorder.step(f"order result: {order_status}")
    if order_status == "paid":
        return session.recorder.finish("paid", f"by {method}")
    if order_status == "payment_failed":
        return session.recorder.finish("declined", f"by {method}")
    raise PageBroken(f"unexpected order status {order_status!r}")


async def run(session: Session) -> Outcome:
    await session.sign_in_as_demo_customer()
    await session.think()

    wanted = session.rng.randint(1, 4)
    added, saw_409 = 0, False
    for _ in range(wanted + 2):                 # a couple of spare tries for skipped products
        result = await find_and_add_one(session)
        if result == "unavailable":
            return session.recorder.finish("unavailable", "the API returned 503 while shopping")
        saw_409 = saw_409 or result == "out_of_stock"
        added += result == "added"
        await session.think()
        if added >= wanted:
            break

    if added == 0:
        return session.recorder.finish("out_of_stock" if saw_409 else "abandoned",
                                       "nothing could be added to the cart")

    await go_to_checkout(session)
    return await place_order(session)
