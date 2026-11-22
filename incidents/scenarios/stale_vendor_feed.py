"""Stale vendor feed: the vendor stops updating.

Story: the vendor's daily update job silently stops (in our setup: the
vendor-day workflow is paused). Their database stays up and reachable, but
prices stop changing. After 36 hours the data contract's freshness rule fails
and ShopLite's ETL refuses the feed.

What it does: nothing automated (decided with the owner: no GitHub
credentials in any script). It tells you to set the repository variable
VENDOR_ACTIVITY_ENABLED to false, waits for you to confirm, and records the
ground truth. It's a multi-day incident, so it doesn't stay in the
foreground: it records its start and exits, and stays active until you run
'revert stale-vendor-feed', which tells you to set the variable back to true
(or delete it) and records the end.

Timing: vendor-day runs every day at 12:00 UTC; the ETL at 14:00 on weekdays.
Pause it on a Thursday after 12:00 and the ETL fails on Monday (the newest
price is then about 98 hours old); pause it on a Monday and it fails on
Wednesday. The vendor-feed-stale alert fires as soon as the collector's
check sees more than 36 hours.

Detection ('status', Mac copy): the vendor's newest price change is more than
24 hours old.
"""

from framework import Context, Incident, Refused

VARIABLE = "VENDOR_ACTIVITY_ENABLED"
FRESHNESS = "SELECT EXTRACT(EPOCH FROM (now() - max(updated_at))) / 3600 FROM supply.prices"


class StaleVendorFeed(Incident):
    name = "stale-vendor-feed"
    title = "Stale vendor feed: the vendor's daily updates stop"
    runs_on = ("mac",)
    foreground = False
    default_minutes = 1              # not used: it lasts until reverted
    max_minutes = 1
    needs_traffic = False
    root_cause = ("The vendor stopped updating their data (the vendor-day job stopped: VENDOR_ACTIVITY_ENABLED was "
                  "set to false). Their database stays reachable, but supply.prices.updated_at stops moving; after "
                  "36 hours the data contract's freshness rule fails and the ETL refuses the feed.")
    correct_fix = ("Contact the vendor to restart their update job (here: set VENDOR_ACTIVITY_ENABLED back to true "
                   "and run vendor-day.yml by hand), then re-run the ETL. Don't loosen the 36-hour rule to make the "
                   "error go away: loading stale prices as if current is worse than loading nothing.")
    expected_signals = [
        "alert shoplite-ops-vendor-feed-stale: FreshnessHours over 36, vendor still reachable",
        "alert shoplite-ops-etl-failed: ErrorStage validate, 'newest updated_at is ... hours old (contract maximum 36)'",
        "VendorFeed_CL: FreshnessHours climbing steadily for days; Reachable true",
    ]

    def _hours_since_update(self, ctx: Context) -> float | None:
        import psycopg
        if not ctx.settings.vendor_admin_url:
            raise Refused("No vendor admin URL. Locally it's built from the root .env's POSTGRES_USER, "
                          "POSTGRES_PASSWORD and POSTGRES_DB; in production set VENDOR_ADMIN_URL in "
                          "incidents/.env.prod and run with --env-file incidents/.env.prod (Mac only).")
        with psycopg.connect(ctx.settings.vendor_admin_url, connect_timeout=15) as conn:
            value = conn.execute(FRESHNESS).fetchone()[0]
        return None if value is None else float(value)

    def start(self, ctx: Context) -> None:
        def pause():
            print(f"\n  In GitHub: repository -> Settings -> Secrets and variables -> Actions -> Variables ->\n"
                  f"  {VARIABLE} -> set it to: false   (create it if it doesn't exist)")
            if input("  Type DONE when it's set: ").strip().upper() != "DONE":
                raise Refused("Not confirmed.")
        ctx.do(f"pause the vendor's daily updates: set the GitHub variable {VARIABLE} to false (by hand)", pause)

    def revert(self, ctx: Context) -> None:
        def resume():
            print(f"\n  In GitHub: set {VARIABLE} back to: true (or delete it), then Actions -> Vendor day ->\n"
                  "  Run workflow, so prices are fresh again. Then run the ETL job by hand on the VM.")
            input("  Press Enter when it's done: ")
        ctx.do(f"resume the vendor's daily updates: set {VARIABLE} to true (by hand) and run vendor-day once", resume)

    def detect(self, ctx: Context) -> str | None:
        hours = self._hours_since_update(ctx)
        if hours is not None and hours > 24:
            return f"the vendor's newest price change is {hours:.0f} hours old ({VARIABLE} still false?)"
        return None


INCIDENT = StaleVendorFeed()
