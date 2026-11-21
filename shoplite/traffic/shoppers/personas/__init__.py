"""The shopper personas. Each module has `async def run(session) -> Outcome`.

PERSONAS maps the names used in SHOPPERS_WEIGHTS to those functions. The
runner (run.py) picks one per session by weight.
"""

from personas import admin, browser, buyer, loyal, searcher

PERSONAS = {
    "browser": browser.run,
    "searcher": searcher.run,
    "buyer": buyer.run,
    "loyal": loyal.run,
    "admin": admin.run,
}

# Personas whose sessions involve a secret: no trace is ever recorded for them.
NO_TRACE = {"admin"}
