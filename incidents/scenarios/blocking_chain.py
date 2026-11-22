"""Blocking chain: a forgotten open transaction.

Story: a DBA, connected with SSMS on the server, corrects some inventory rows
in an explicit transaction and goes to lunch without committing. Their update
holds exclusive locks on the inventory rows of the best-selling products and
of the catalog's first page; every shopper who browses those products or
checks them out waits behind it, then times out after 30 seconds (503).

What it does: opens a session as the SQL admin (Windows authentication on the
VM) with SSMS's program name, runs an UPDATE that changes nothing
(reorder_level = reorder_level) but takes the locks, and leaves the
transaction open until the deadline, then rolls it back. No data changes.

Session-only: if the injector dies, its session ends, SQL Server rolls the
transaction back and the locks go with it, so there's nothing to detect later.
"""

from framework import Context, Incident

PROGRAM = "Microsoft SQL Server Management Studio - Query"

# The rows shoppers touch most: the 10 best sellers of the last 7 days and the
# 24 products on the catalog's first page (default sort by name).
HOT_PRODUCTS = """
SELECT product_id FROM (
    SELECT TOP (10) oi.product_id
    FROM dbo.order_items AS oi JOIN dbo.orders AS o ON o.order_id = oi.order_id
    WHERE o.created_at > DATEADD(DAY, -7, SYSUTCDATETIME())
    GROUP BY oi.product_id ORDER BY COUNT(*) DESC) AS best_sellers
UNION
SELECT product_id FROM (
    SELECT TOP (24) product_id FROM dbo.products WHERE is_active = 1 ORDER BY name, product_id) AS first_page;
"""


class BlockingChain(Incident):
    name = "blocking-chain"
    title = "Blocking chain: an open transaction holds hot inventory rows"
    runs_on = ("vm",)
    default_minutes = 15
    max_minutes = 30
    root_cause = ("An interactive SSMS session (the SQL admin) ran an UPDATE on dbo.inventory inside an explicit "
                  "transaction and never committed. Its exclusive row locks on the best sellers and the catalog's "
                  "first page block shoppers' reads and checkouts; the head blocker is sleeping with an open "
                  "transaction.")
    correct_fix = ("Identify the head blocker (sleeping session, open transaction, last statement UPDATE "
                   "dbo.inventory), confirm with its owner, then have it COMMIT or ROLL BACK, or KILL the session. "
                   "Prevention: no manual changes in open transactions on production; consider READ COMMITTED "
                   "SNAPSHOT so readers don't wait for writers.")
    expected_signals = [
        "alert shoplite-sql-blocking (blocking over 30 s)",
        "SqlBlocking_CL: head_blocker sleeping, OpenTransactions 1, TransactionSeconds growing, "
        "StatementText UPDATE dbo.inventory; many blocked ShopLite-API sessions waiting on LCK_M_S / LCK_M_U",
        "SqlXEvents_CL: blocked_process_report every 10 s",
        "SqlWaitStats_CL: LCK_M_* waits jump",
        "API: p95 latency and 503s on catalog, product, and checkout requests",
    ]

    def __init__(self):
        self.connection = None

    def start(self, ctx: Context) -> None:
        reader = ctx.sql(PROGRAM)
        try:
            ids = [row[0] for row in reader.cursor().execute(HOT_PRODUCTS).fetchall()]
        finally:
            reader.close()
            ctx.sessions.pop()          # the lookup isn't the blocking session
        ctx.details["product_ids"] = ids
        statement = ("UPDATE dbo.inventory SET reorder_level = reorder_level "
                     f"WHERE product_id IN ({', '.join(str(int(i)) for i in ids)});")

        def open_transaction():
            # autocommit off: the UPDATE starts a transaction that stays open.
            self.connection = ctx.sql(PROGRAM, autocommit=False)
            self.connection.cursor().execute(statement)

        ctx.do(f"as the SQL admin in 'SSMS', update {len(ids)} hot inventory rows inside a transaction "
               "and leave it open", open_transaction)

    def revert(self, ctx: Context) -> None:
        if self.connection is not None:
            ctx.do("roll back the open transaction and disconnect", self._close)
        else:
            ctx.note("no open session from this run (if the injector died, SQL Server already rolled it back)")

    def _close(self) -> None:
        try:
            self.connection.rollback()
        finally:
            self.connection.close()
            self.connection = None


INCIDENT = BlockingChain()
