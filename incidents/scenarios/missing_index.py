"""Missing index: product pages slow down.

Story: a maintenance script disabled an index to speed up a bulk load of
order history and never rebuilt it. Disabling a nonclustered index keeps its
definition but throws away its data, so queries can't use it.

The index: IX_order_items_product_id on dbo.order_items (product_id), which
every product page's "often bought together" query needs to find the orders
containing that product (shoplite/api/app/routers/catalog.py). Without it,
each product page view scans the whole order_items table (millions of rows):
product pages become slow, CPU rises, and the query's plan changes in Query
Store.

Persistent: the index stays disabled until reverted, even if the injector or
the VM stops. 'status' detects it; 'revert' rebuilds it (online if possible,
so checkouts can keep inserting order items meanwhile).
"""

from framework import Context, Incident, Refused

PROGRAM = "SQLCMD"
INDEX, TABLE = "IX_order_items_product_id", "dbo.order_items"

INDEX_STATE = """
SELECT i.is_disabled FROM sys.indexes AS i
WHERE i.name = ? AND i.object_id = OBJECT_ID(?);
"""


class MissingIndex(Incident):
    name = "missing-index"
    title = "Missing index: the 'often bought together' index is disabled"
    runs_on = ("vm",)
    default_minutes = 20
    max_minutes = 45
    root_cause = (f"Index {INDEX} on {TABLE} was disabled (ALTER INDEX ... DISABLE) by a maintenance script and "
                  "never rebuilt. The product page's 'often bought together' query can no longer seek by "
                  "product_id and scans dbo.order_items on every product view.")
    correct_fix = (f"ALTER INDEX {INDEX} ON {TABLE} REBUILD (WITH (ONLINE = ON) where possible). Prevention: "
                   "maintenance scripts that disable indexes must rebuild them in the same run; monitor "
                   "sys.indexes.is_disabled.")
    expected_signals = [
        "alert shoplite-api-latency (p95 of GET /api/products/{id} jumps)",
        "SqlTopQueries_CL: the bought-together query's duration and logical reads jump, with a new plan "
        "(PlansForQuery or PlanHash change)",
        "SqlXEvents_CL: rpc_completed events over 2 s for the product detail query",
        "SqlWaitStats_CL / Perf: more CPU (SOS_SCHEDULER_YIELD, sqlservr CPU) and possibly PAGEIOLATCH_SH",
        "CPU credits draining faster than usual",
    ]

    def _disabled(self, ctx: Context) -> bool | None:
        connection = ctx.sql(PROGRAM)
        try:
            return ctx.scalar(connection, INDEX_STATE, INDEX, TABLE)
        finally:
            connection.close()
            ctx.sessions.pop()

    def preflight(self, ctx: Context) -> None:
        state = self._disabled(ctx)
        if state is None:
            raise Refused(f"{INDEX} doesn't exist on {TABLE}.")
        if state:
            raise Refused(f"{INDEX} is already disabled: run 'revert missing-index' first.")

    def start(self, ctx: Context) -> None:
        def disable():
            connection = ctx.sql(PROGRAM)
            try:
                connection.cursor().execute(f"ALTER INDEX {INDEX} ON {TABLE} DISABLE;")
            finally:
                connection.close()
        ctx.do(f"as the SQL admin ('SQLCMD'), disable {INDEX} on {TABLE}", disable)

    def revert(self, ctx: Context) -> None:
        if not self._disabled(ctx):
            ctx.note(f"{INDEX} is enabled: nothing to revert")
            return

        def rebuild():
            connection = ctx.sql(PROGRAM)
            connection.timeout = 0                     # a rebuild of millions of rows can take minutes
            try:
                try:
                    connection.cursor().execute(f"ALTER INDEX {INDEX} ON {TABLE} REBUILD WITH (ONLINE = ON);")
                except Exception:
                    # Some versions can't rebuild a DISABLED index online: rebuild offline
                    # instead (order_items is locked for the seconds it takes).
                    connection.cursor().execute(f"ALTER INDEX {INDEX} ON {TABLE} REBUILD;")
            finally:
                connection.close()
        ctx.do(f"rebuild {INDEX} on {TABLE}", rebuild)

    def detect(self, ctx: Context) -> str | None:
        return f"{INDEX} on {TABLE} is DISABLED" if self._disabled(ctx) else None


INCIDENT = MissingIndex()
