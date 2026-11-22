"""Plan regression: a forced plan that's wrong for most inputs.

Story: during a tuning experiment, a DBA forced a Query Store plan for the
sales report and forgot to remove it. The forced plan was compiled for a
ONE-DAY range (seek the orders index, look each row up); the admin reports
usually ask for months or years, where that plan does millions of lookups
instead of one scan. Classic parameter sniffing, made permanent by forcing.

What it does:
  1. finds the sales report's day-grouping query in Query Store (it must have
     run at least once; the injector runs the report once through the API to
     make sure);
  2. recompiles dbo.usp_report_sales and runs it for a single day, so Query
     Store records a plan made for a tiny range;
  3. forces that plan (sp_query_store_force_plan);
  4. while it lasts, asks the API for the two-year daily sales report every 30
     seconds, as an admin in a browser would: slow, sometimes timing out.
Revert: unforce every forced plan of dbo.usp_report_sales and recompile.

Persistent: a forced plan survives restarts; 'status' detects it.
"""

from datetime import date, timedelta

from framework import Context, Incident, Refused

PROGRAM = "Microsoft SQL Server Management Studio - Query"
PROCEDURE = "dbo.usp_report_sales"

FIND_QUERY = """
SELECT TOP (1) q.query_id
FROM sys.query_store_query AS q
JOIN sys.query_store_query_text AS t ON t.query_text_id = q.query_text_id
WHERE q.object_id = OBJECT_ID(N'dbo.usp_report_sales')
  AND t.query_sql_text LIKE N'%GROUP BY CAST(o.created_at AS DATE)%'
ORDER BY q.last_execution_time DESC;
"""
NEWEST_PLAN = """
SELECT TOP (1) plan_id FROM sys.query_store_plan WHERE query_id = ? ORDER BY last_execution_time DESC;
"""
FORCED_PLANS = """
SELECT p.query_id, p.plan_id
FROM sys.query_store_plan AS p
JOIN sys.query_store_query AS q ON q.query_id = p.query_id
WHERE q.object_id = OBJECT_ID(N'dbo.usp_report_sales') AND p.is_forced_plan = 1;
"""


class PlanRegression(Incident):
    name = "plan-regression"
    title = "Plan regression: a forced one-day plan for the sales report"
    runs_on = ("vm",)
    default_minutes = 20
    max_minutes = 40
    root_cause = ("A Query Store plan was forced for dbo.usp_report_sales (the daily sales query). It was compiled "
                  "for a one-day range (index seek plus key lookups) and is far too expensive for the usual "
                  "multi-month report ranges: parameter sniffing made permanent by plan forcing.")
    correct_fix = ("Unforce the plan (EXEC sp_query_store_unforce_plan @query_id, @plan_id) and recompile the "
                   "procedure. Longer term: OPTION (RECOMPILE) or OPTIMIZE FOR on the report query, or separate "
                   "procedures for short and long ranges; only force plans with a reviewed reason.")
    expected_signals = [
        "SqlTopQueries_CL: usp_report_sales' day query with a different PlanHash, far higher duration and "
        "logical reads per execution",
        "SqlXEvents_CL: rpc_completed over 2 s (often 20-30 s) from ShopLite-API for the report",
        "API: GET /api/admin/reports/sales slow, some 503s (query timeout); CPU and CPU credits",
        "SqlTopQueries_CL: IsForcedPlan = true on that query's plan (Query Store's is_forced_plan)",
    ]

    def __init__(self):
        self.query_id = None

    # ---- helpers ----
    def _admin(self, ctx: Context, sql: str, *params, fetch: bool = True):
        """One statement as the 'DBA in SSMS'. Changes (fetch=False) keep their
        session in the ground truth's injected sessions; plain reads don't."""
        connection = ctx.sql(PROGRAM)
        try:
            cursor = connection.cursor().execute(sql, *params)
            return cursor.fetchall() if fetch else None
        finally:
            connection.close()
            if fetch:
                ctx.sessions.pop()

    def _report(self, ctx: Context, days: int) -> float:
        """Ask the API for the daily sales report over the last `days` days; seconds taken."""
        import time
        import requests
        end = date.today()
        started = time.monotonic()
        try:
            requests.get(f"{ctx.settings.api_base_url}/api/admin/reports/sales",
                         params={"start_date": str(end - timedelta(days=days)), "end_date": str(end),
                                 "group_by": "day"},
                         headers={"X-Admin-Token": ctx.api_setting("ADMIN_TOKEN"),
                                  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/131.0"},
                         timeout=60)
        except requests.RequestException:
            pass                                    # a timeout is part of the incident
        return time.monotonic() - started

    # ---- the incident ----
    def preflight(self, ctx: Context) -> None:
        rows = self._admin(ctx, FIND_QUERY)
        if not rows:
            ctx.note("the report's query isn't in Query Store yet: running the report once through the API")
            self._report(ctx, 730)
            rows = self._admin(ctx, FIND_QUERY)
        if not rows:
            raise Refused("Query Store hasn't captured the sales report's daily query yet. Run the admin sales "
                          "report (two years, by day) a few times, then try again.")
        self.query_id = rows[0][0]
        if self._admin(ctx, FORCED_PLANS):
            raise Refused("A plan is already forced for dbo.usp_report_sales: run 'revert plan-regression' first.")

    def start(self, ctx: Context) -> None:
        one_day = (str(date.today() - timedelta(days=1)), str(date.today()))
        ctx.do(f"recompile {PROCEDURE}",
               lambda: self._admin(ctx, "EXEC sp_recompile N'dbo.usp_report_sales';", fetch=False))
        ctx.do("run the report for a single day, so Query Store records a plan made for a tiny range",
               lambda: self._admin(ctx, "EXEC dbo.usp_report_sales @start_date = ?, @end_date = ?, @group_by = 'day';",
                                   *one_day))
        if ctx.dry_run:
            ctx.do(f"force that plan for query {self.query_id}, then request the two-year report every 30 s")
            return
        plan_id = self._admin(ctx, NEWEST_PLAN, self.query_id)[0][0]
        ctx.details.update({"query_id": self.query_id, "plan_id": plan_id})
        ctx.do(f"force plan {plan_id} for query {self.query_id} (sp_query_store_force_plan)",
               lambda: self._admin(ctx, "EXEC sp_query_store_force_plan @query_id = ?, @plan_id = ?;",
                                   self.query_id, plan_id, fetch=False))

    def hold(self, ctx: Context) -> None:
        while not ctx.should_stop():
            seconds = self._report(ctx, 730)
            ctx.note(f"two-year daily sales report took {seconds:.1f} s")
            ctx.wait(max(0.0, 30 - seconds))

    def revert(self, ctx: Context) -> None:
        forced = self._admin(ctx, FORCED_PLANS)
        for query_id, plan_id in forced:
            ctx.do(f"unforce plan {plan_id} for query {query_id}",
                   lambda q=query_id, p=plan_id: self._admin(
                       ctx, "EXEC sp_query_store_unforce_plan @query_id = ?, @plan_id = ?;", q, p, fetch=False))
        ctx.do(f"recompile {PROCEDURE}",
               lambda: self._admin(ctx, "EXEC sp_recompile N'dbo.usp_report_sales';", fetch=False))

    def detect(self, ctx: Context) -> str | None:
        forced = self._admin(ctx, FORCED_PLANS)
        return (f"{len(forced)} forced Query Store plan(s) on {PROCEDURE}: "
                + ", ".join(f"query {q} plan {p}" for q, p in forced)) if forced else None


INCIDENT = PlanRegression()
