"""Store admin persona (~2%): checks stock and runs a sales report.

Signs in on /admin with SHOPLITE_ADMIN_TOKEN, opens inventory, switches to the
low-stock view, then opens /admin/reports (which runs both reports for the
last 30 days on load) and runs them again for a random 7-90 day range within
the last year, with a random grouping and top-N.

The reports are the heaviest queries ShopLite has, which is why this persona
is rare. Ends "admin_done" or "unavailable".

SECRET HANDLING: the token is typed into a password box and sent as a header.
The runner never records a trace for this persona (traces capture typed text
and headers) and blanks the token box before any screenshot. Nothing here
prints the token.
"""

from datetime import date, timedelta

from helpers import Outcome, PageBroken, Session, wait_for_api

GROUPINGS = ("day", "product", "state")


async def run(session: Session) -> Outcome:
    rng = session.rng
    token = session.settings.admin_token
    if not token:
        raise PageBroken("SHOPLITE_ADMIN_TOKEN is empty; the admin persona can't sign in")

    # --- Sign in -----------------------------------------------------------------
    await session.goto("/admin")
    box = await session.expect("admin-token-input")
    await box.fill(token)
    mark = session.api.mark()
    await (await session.expect("admin-signin-submit")).click()
    status = await wait_for_api(session, "GET", "/api/admin/inventory", mark)
    if status == 401:
        raise PageBroken("the API rejected SHOPLITE_ADMIN_TOKEN (401); check the GitHub secret")
    if status == 503:
        return session.recorder.finish("unavailable", "admin sign-in returned 503")
    await session.expect("admin-signout")
    session.recorder.step("signed in to admin")
    await session.think()

    # --- Inventory, then low stock -------------------------------------------------
    mark = session.api.mark()
    await session.goto("/admin/inventory")
    if await wait_for_api(session, "GET", "/api/admin/inventory", mark) == 503:
        return session.recorder.finish("unavailable", "inventory returned 503")
    await session.expect("inventory-row")
    session.recorder.step("opened inventory")
    await session.think()

    mark = session.api.mark()
    await (await session.expect("low-stock-toggle")).click()
    if await wait_for_api(session, "GET", "/api/admin/inventory", mark) == 503:
        return session.recorder.finish("unavailable", "low-stock list returned 503")
    session.recorder.step("showed low stock only")
    await session.think()

    # --- Reports -----------------------------------------------------------------------
    mark = session.api.mark()
    await session.goto("/admin/reports")
    # On load the page runs both reports for the last 30 days.
    if await wait_for_api(session, "GET", "/api/admin/reports/sales", mark,
                          timeout_ms=session.settings.step_timeout_ms * 3) == 503:
        return session.recorder.finish("unavailable", "the default report returned 503")
    await session.expect("report-run")
    session.recorder.step("opened reports (last 30 days)")
    await session.think()

    days = rng.randint(7, 90)
    end = date.today() - timedelta(days=rng.randint(0, 365 - days))
    start = end - timedelta(days=days - 1)
    grouping = rng.choice(GROUPINGS)
    await (await session.expect("report-start-date")).fill(start.isoformat())
    await (await session.expect("report-end-date")).fill(end.isoformat())
    await (await session.expect("report-group-by")).select_option(grouping)
    await (await session.expect("top-n-input")).fill(str(rng.choice((5, 10, 20))))
    await session.think()

    mark = session.api.mark()
    await (await session.expect("report-run")).click()
    # A report over a large range can take several seconds on the burstable VM.
    status = await wait_for_api(session, "GET", "/api/admin/reports/sales", mark,
                                timeout_ms=session.settings.step_timeout_ms * 3)
    if status == 503:
        return session.recorder.finish("unavailable", "the sales report returned 503")
    if status >= 300:
        raise PageBroken(f"the sales report returned HTTP {status}")
    session.recorder.step(f"ran the sales report: {start} to {end} by {grouping}")
    await session.think()

    return session.recorder.finish("admin_done", f"{days}-day report by {grouping}")
