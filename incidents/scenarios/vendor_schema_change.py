"""Vendor schema change: the vendor renames a column.

Story: the vendor's engineers ship a release that renames supply.prices.unit_cost
to unit_price, without telling ShopLite. The next ETL run's contract check
finds the column missing and refuses the feed; SalesDB keeps yesterday's
supplier prices.

What it does: as the vendor's ADMIN (the owner of the vendor database, which
is who would make such a change), ALTER TABLE supply.prices RENAME COLUMN
unit_cost TO unit_price. Revert renames it back. Grants and data are kept
(renaming a column keeps both).

Runs from the Mac: the vendor admin connection never goes on the VM. The ETL
runs at 14:00 UTC; to see the failure inside the incident, either let the
incident span 14:00, or start the ETL job by hand on the VM while it's active
(EXEC msdb.dbo.sp_start_job N'ShopLite - Vendor ETL'). Avoid 12:00 UTC: the
vendor's own daily changes (vendor-day.yml) would fail too.

Persistent: the rename stays until reverted; 'status' (on the Mac) detects it.
"""

from framework import Context, Incident, Refused

OLD, NEW = "unit_cost", "unit_price"

COLUMNS = """
SELECT column_name FROM information_schema.columns
WHERE table_schema = 'supply' AND table_name = 'prices' AND column_name IN (%s, %s)
"""


class VendorSchemaChange(Incident):
    name = "vendor-schema-change"
    title = "Vendor schema change: supply.prices.unit_cost renamed"
    runs_on = ("mac",)
    default_minutes = 45
    max_minutes = 120
    needs_traffic = False
    root_cause = ("The vendor renamed supply.prices.unit_cost to unit_price in their Postgres database without "
                  "notice. ShopLite's ETL validates the vendor against the data contract (1.0.0) and refuses the "
                  "feed at the validate stage: column missing.")
    correct_fix = ("Contact the vendor: either they restore unit_cost (or add it back as a column alongside "
                   "unit_price), or ShopLite updates the data contract (a MAJOR version) and the ETL's extract "
                   "query in the same change, then re-runs the ETL. Nothing to fix inside SQL Server.")
    expected_signals = [
        "alert shoplite-ops-etl-failed: ErrorStage validate, 'supply.prices.unit_cost: column missing (renamed or "
        "removed?)'",
        "alert shoplite-sql-job-failed for 'ShopLite - Vendor ETL' (exit code 12)",
        "OpsEtlRuns_CL: Status failed, RowsRead null; VendorFeed_CL still reachable and fresh (the vendor is up)",
    ]

    def _connect(self, ctx: Context):
        import psycopg
        if not ctx.settings.vendor_admin_url:
            raise Refused("VENDOR_ADMIN_URL isn't set in incidents/.env (Mac copy).")
        return psycopg.connect(ctx.settings.vendor_admin_url, connect_timeout=15, autocommit=True)

    def _columns(self, ctx: Context) -> set[str]:
        with self._connect(ctx) as conn:
            return {row[0] for row in conn.execute(COLUMNS, (OLD, NEW)).fetchall()}

    def preflight(self, ctx: Context) -> None:
        columns = self._columns(ctx)
        if NEW in columns:
            raise Refused(f"supply.prices already has {NEW}: run 'revert vendor-schema-change' first.")
        if OLD not in columns:
            raise Refused(f"supply.prices has no {OLD} column.")

    def start(self, ctx: Context) -> None:
        def rename():
            with self._connect(ctx) as conn:
                conn.execute(f"ALTER TABLE supply.prices RENAME COLUMN {OLD} TO {NEW}")
        ctx.do(f"as the vendor's admin, rename supply.prices.{OLD} to {NEW}", rename)

    def revert(self, ctx: Context) -> None:
        if NEW not in self._columns(ctx):
            ctx.note(f"supply.prices has {OLD}: nothing to revert")
            return

        def rename_back():
            with self._connect(ctx) as conn:
                conn.execute(f"ALTER TABLE supply.prices RENAME COLUMN {NEW} TO {OLD}")
        ctx.do(f"rename supply.prices.{NEW} back to {OLD} (then run the ETL job by hand to load today's feed)",
               rename_back)

    def detect(self, ctx: Context) -> str | None:
        return f"supply.prices.{OLD} is renamed to {NEW}" if NEW in self._columns(ctx) else None


INCIDENT = VendorSchemaChange()
