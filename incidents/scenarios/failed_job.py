"""Failed job: a job step edited to call something that isn't there.

Story: a DBA, preparing the next ReportingDB release, edits the SQL Agent job
"ShopLite - ReportingDB refresh" in SSMS so it calls rpt.usp_refresh_sales_v2,
a procedure that hasn't been deployed yet. Every run now fails.

What it does: as the SQL admin ("SSMS"), changes the job's step to call
rpt.usp_refresh_sales_v2, then starts the job once, so the failure happens
during the incident instead of at tomorrow's 14:30 run. Revert restores the
step's command (EXECUTE rpt.usp_refresh_sales;) and runs the job once more, so
ReportingDB catches up. Backups are never touched.

Persistent: the broken step stays until reverted; 'status' (on the VM)
detects it.
"""

from framework import Context, Incident, Refused

PROGRAM = "Microsoft SQL Server Management Studio"
JOB = "ShopLite - ReportingDB refresh"
GOOD = "EXECUTE rpt.usp_refresh_sales;"
BROKEN = "EXECUTE rpt.usp_refresh_sales_v2;"

STEP_COMMAND = """
SELECT s.command FROM msdb.dbo.sysjobsteps AS s
JOIN msdb.dbo.sysjobs AS j ON j.job_id = s.job_id
WHERE j.name = ? AND s.step_id = 1;
"""


class FailedJob(Incident):
    name = "failed-job"
    title = "Failed job: the ReportingDB refresh calls a procedure that doesn't exist"
    runs_on = ("vm",)
    default_minutes = 15
    max_minutes = 30
    needs_traffic = False
    root_cause = (f"The SQL Agent job '{JOB}' was edited (step 1) to run rpt.usp_refresh_sales_v2, which doesn't "
                  "exist in ReportingDB, so every run fails with 'Could not find stored procedure'.")
    correct_fix = (f"Change the job step back to '{GOOD}' (or deploy the new procedure first), then run the job once "
                   "to catch up. Job definitions belong in server/jobs/sql/040_jobs.sql: re-running it restores "
                   "every job exactly as defined.")
    expected_signals = [
        "alert shoplite-sql-job-failed: JobName 'ShopLite - ReportingDB refresh', FailedStepMessage "
        "\"Could not find stored procedure 'rpt.usp_refresh_sales_v2'\"",
        "SqlAgentJobRuns_CL: Outcome failed",
        "OpsRefreshRuns_CL: no new refresh (the procedure never started, so no failed refresh row either)",
    ]

    def _command(self, ctx: Context) -> str | None:
        connection = ctx.sql(PROGRAM, database="msdb")
        try:
            return ctx.scalar(connection, STEP_COMMAND, JOB)
        finally:
            connection.close()
            ctx.sessions.pop()

    def _admin(self, ctx: Context, sql: str, *params) -> None:
        connection = ctx.sql(PROGRAM, database="msdb")
        try:
            connection.cursor().execute(sql, *params)
        finally:
            connection.close()

    def preflight(self, ctx: Context) -> None:
        command = self._command(ctx)
        if command is None:
            raise Refused(f"Job '{JOB}' (step 1) doesn't exist: run server/jobs/sql/040_jobs.sql.")
        if command.strip() == BROKEN:
            raise Refused("The job step is already broken: run 'revert failed-job' first.")

    def start(self, ctx: Context) -> None:
        ctx.details["original_command"] = self._command(ctx)
        ctx.do(f"as the SQL admin in 'SSMS', change step 1 of '{JOB}' to: {BROKEN}",
               lambda: self._admin(ctx, "EXEC msdb.dbo.sp_update_jobstep @job_name = ?, @step_id = 1, @command = ?;",
                                   JOB, BROKEN))
        ctx.do(f"start '{JOB}' now (it fails)",
               lambda: self._admin(ctx, "EXEC msdb.dbo.sp_start_job @job_name = ?;", JOB))

    def revert(self, ctx: Context) -> None:
        command = self._command(ctx)
        if command is None or command.strip() != BROKEN:
            ctx.note("the job step isn't the broken one: nothing to revert")
            return
        original = ctx.details.get("original_command") or GOOD
        ctx.do(f"restore step 1 of '{JOB}' to: {original.strip()}",
               lambda: self._admin(ctx, "EXEC msdb.dbo.sp_update_jobstep @job_name = ?, @step_id = 1, @command = ?;",
                                   JOB, original))
        ctx.do(f"start '{JOB}' once so ReportingDB catches up",
               lambda: self._admin(ctx, "EXEC msdb.dbo.sp_start_job @job_name = ?;", JOB))

    def detect(self, ctx: Context) -> str | None:
        command = self._command(ctx)
        return f"step 1 of '{JOB}' runs {BROKEN}" if command and command.strip() == BROKEN else None


INCIDENT = FailedJob()
