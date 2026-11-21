"""Loyal customer persona (~8%): signs in, reorders a past order, checks out.

Opens Orders (order history), sometimes filters to delivered orders, opens an
order, presses Reorder, then checks the cart drawer. If the reorder put items
in the cart, checks out with the buyer's payment steps.

Ends "reordered" (paid), "declined", "out_of_stock", "unavailable", or
"nothing_to_reorder" (no past orders, or none of their items could be added).

Exercises the order-history queries (GET /api/orders, /api/orders/{id}) and
the reorder path (POST /api/orders/{id}/reorder) on top of checkout.
"""

from helpers import Outcome, Session, wait_for_api
from personas.buyer import place_order


async def run(session: Session) -> Outcome:
    rng = session.rng
    await session.sign_in_as_demo_customer()
    await session.think()

    mark = session.api.mark()
    await (await session.expect("nav-account-link")).click()
    if await wait_for_api(session, "GET", "/api/orders", mark) == 503:
        return session.recorder.finish("unavailable", "order history returned 503")
    if await session.first_visible("order-row", "orders-empty") == "orders-empty":
        return session.recorder.finish("nothing_to_reorder", "this customer has no orders yet")
    session.recorder.step("opened order history")
    await session.think()

    # Sometimes narrow the list to delivered orders (filters this page only).
    if rng.random() < 0.3:
        delivered = session.page.locator('[data-testid="orders-filter"][data-filter="delivered"]')
        if await delivered.is_visible():
            await delivered.click()
            session.recorder.step("filtered to delivered orders")
            await session.think()
    if not await session.testid("order-link").first.is_visible():
        return session.recorder.finish("nothing_to_reorder", "no orders matched the filter")

    mark = session.api.mark()
    await (await session.pick("order-link")).click()
    if await wait_for_api(session, "GET", "/api/orders/", mark) == 503:
        return session.recorder.finish("unavailable", "the order returned 503")
    await session.expect("reorder-button")
    session.recorder.step("opened a past order")
    await session.think()

    mark = session.api.mark()
    await (await session.expect("reorder-button")).click()
    status = await wait_for_api(session, "POST", "/reorder", mark)
    if status == 503:
        return session.recorder.finish("unavailable", "reorder returned 503")
    await session.expect("reorder-result")
    session.recorder.step(f"pressed Reorder (HTTP {status})")
    await session.think()

    # Did anything land in the cart? (Items can be skipped: out of stock, or
    # no longer sold.) The drawer shows either lines or its empty message.
    await (await session.expect("nav-cart-link")).click()
    if await session.first_visible("cart-drawer-item", "cart-drawer-empty") == "cart-drawer-empty":
        return session.recorder.finish("nothing_to_reorder", "none of the order's items could be added")
    await session.think()
    await (await session.expect("cart-drawer-checkout")).click()
    await session.expect("place-order-button")
    session.recorder.step("reached checkout")

    outcome = await place_order(session)
    if outcome.outcome == "paid":
        return session.recorder.finish("reordered", outcome.detail)
    return outcome
