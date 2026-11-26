"""Generates the Grafana dashboards from the compact definitions below:
dashboards as code. THIS FILE is the source of truth; the JSON it writes is
never committed (git-ignored), so there's only ever one set to import.

Writing Grafana's JSON by hand means hundreds of lines of boilerplate per
dashboard (panel positions, data source blocks, field settings), easy to break
and hard to review. Here each panel is one line or two: a title, a data
source, a KQL query, a unit. Change a panel here, re-run, re-import.

    python observability/tools/make_dashboards.py
        import-ready dashboards in observability/grafana/local/<name>.json,
        with YOUR values as the variable defaults, read from the root .env
        (GRAFANA_* variables; run it in the root .venv). Import these.
    python observability/tools/make_dashboards.py --placeholders
        for anyone without a .env: observability/grafana/placeholders/
        <name>.placeholders.json, variables left as placeholders to fill in
        after import. Reads no .env; any Python 3. The different file names
        are on purpose: Grafana's file picker shows only the name.

ONE data source for everything: Grafana's free Azure Monitor data source.
Grafana's Azure Data Explorer data source is an Enterprise plugin (Grafana
Cloud Pro/Advanced or Enterprise only): it stops working when a trial ends.
So ADX is reached THROUGH Log Analytics, with a cross-service query:

    adx("<cluster URI>/<database>").SqlWaitStats_CL | where ...

Log Analytics passes the caller's identity on to ADX, so Grafana's app
registration needs Log Analytics Reader on the workspace AND Viewer on the
ADX database (observability/adx-grafana-setup.md).

What each panel reads, chosen per panel:
  ADX history (adx())      the collector's tables, for the selected time range;
                           months of history; ingestion lags about 5 minutes
  Log Analytics            near-real-time collector data, and what only lives
                           there: Application Insights (AppRequests, ...) and
                           the agent's Perf, Event, Heartbeat
  Azure metrics            the VM's platform metrics (CPU credits, CPU)

Every dashboard has the same variables, so nothing specific to your Grafana
stack or Azure subscription is written into the files:
  azmon           the Azure Monitor data source (picked from a list after import)
  workspace       the Log Analytics workspace's resource ID
  subscription    the Azure subscription ID (for the VM metrics)
  adx_cluster     the ADX cluster URI, e.g. https://<name>.<region>.kusto.windows.net
  adx_database    the ADX database: shoplite (shoplite_dev to test against test data)

If cross-service queries ever stop working, ADX's own dashboards (in the ADX
web UI) read the same tables directly: observability/grafana/README.md.

KQL macros the data sources fill in: $__timeFilter(TimeGenerated) (the
dashboard's time range) and $__timeInterval / $__interval (a bin size that
fits the panel's width). Event tables are de-duplicated by their unique_id
(at-least-once delivery, observability/schema.yml).
"""

import argparse
import copy
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GRAFANA_DIR = REPO_ROOT / "observability" / "grafana"
LOCAL_OUT = GRAFANA_DIR / "local"                  # default: your values (git-ignored)
PLACEHOLDERS_OUT = GRAFANA_DIR / "placeholders"    # --placeholders (git-ignored too)

AZMON_TYPE = "grafana-azure-monitor-datasource"
VM = {"resourceGroup": "rg-shoplite-prod", "resourceName": "prod-sql-01",
      "metricNamespace": "microsoft.compute/virtualmachines", "region": "northcentralus"}


# -----------------------------------------------------------------------------
# Targets (one query each)
# -----------------------------------------------------------------------------
# The ADX database, as Log Analytics' adx() function addresses it (both parts are variables).
ADX_SOURCE = 'adx("${adx_cluster}/${adx_database}")'
# A collector table name (they all end in _CL) not already preceded by "." (already wrapped).
COLLECTOR_TABLE = re.compile(r"(?<![.\w])([A-Za-z][A-Za-z0-9]*_CL)\b")


def adx(kql: str, table: bool = False) -> dict:
    """A query on ADX's history, sent through Log Analytics: every collector
    table is wrapped as adx("<cluster>/<database>").<table>. The panels below
    stay written as plain KQL (SqlWaitStats_CL | ...), easy to read and to
    paste into the ADX web UI; the wrapping happens here, so none is missed.

    Every ADX query MUST filter time itself ($__timeFilter(TimeGenerated)):
    "The Log Analytics Query API doesn't pass the time filter" to ADX
    (Microsoft, "Correlate data in Azure Data Explorer ... with data in a Log
    Analytics workspace"). Without it a panel would read all 12 months."""
    return la(COLLECTOR_TABLE.sub(lambda m: f"{ADX_SOURCE}.{m.group(1)}", kql), table)


def la(kql: str, table: bool = False) -> dict:
    return {"refId": "A", "datasource": {"type": AZMON_TYPE, "uid": "${azmon}"},
            "queryType": "Azure Log Analytics",
            "azureLogAnalytics": {"query": kql.strip(), "resources": ["${workspace}"],
                                  "resultFormat": "table" if table else "time_series"}}


def metric(name: str, aggregation: str = "Average") -> dict:
    return {"refId": "A", "datasource": {"type": AZMON_TYPE, "uid": "${azmon}"},
            "queryType": "Azure Monitor", "subscription": "${subscription}",
            "azureMonitor": {"resources": [{"subscription": "${subscription}", **VM}],
                             "metricNamespace": VM["metricNamespace"], "metricName": name,
                             "aggregation": aggregation, "timeGrain": "auto"}}


# -----------------------------------------------------------------------------
# Panels
# -----------------------------------------------------------------------------
def panel(kind: str, title: str, target: dict, width: int, height: int, unit: str = "short",
          description: str = "", options: dict | None = None, thresholds: list | None = None,
          overrides: list | None = None) -> dict:
    defaults = {"unit": unit}
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute",
                                  "steps": [{"color": color, "value": value} for value, color in thresholds]}
        defaults["color"] = {"mode": "thresholds"}
    return {"type": kind, "title": title, "description": description, "datasource": target["datasource"],
            "targets": [target], "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": options or {}, "_size": (width, height)}


def ts(title, target, unit="short", width=12, description="", stacked=False):
    options = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}
    p = panel("timeseries", title, target, width, 8, unit, description, options)
    if stacked:
        p["fieldConfig"]["defaults"]["custom"] = {"stacking": {"mode": "normal"}, "fillOpacity": 30}
    return p


def stat(title, target, unit="short", width=4, description="", thresholds=None):
    options = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "textMode": "value"}
    return panel("stat", title, target, width, 4, unit, description, options, thresholds)


def table(title, target, width=24, height=8, description="", status_column=None):
    """status_column: a column of outcomes (succeeded, failed, ...) to colour like everywhere else."""
    overrides = [status_cells(status_column)] if status_column else None
    return panel("table", title, target, width, height, "short", description, {"showHeader": True},
                 overrides=overrides)


def bars(title, target, unit="short", width=12, description="", status_series=False):
    """Stacked daily bars: a time series drawn as bars, one colour per series.
    status_series: the series are outcomes; colour them by meaning (STATUS_COLORS)."""
    p = ts(title, target, unit, width, description)
    p["fieldConfig"]["defaults"]["custom"] = {"drawStyle": "bars", "fillOpacity": 80,
                                              "stacking": {"mode": "normal"}}
    if status_series:
        p["fieldConfig"]["overrides"] = status_series_colors()
    return p


# -----------------------------------------------------------------------------
# Status colours: the same meaning, the same colour, on every panel
# -----------------------------------------------------------------------------
# Without these, Grafana colours series from its palette in the order they
# arrive, so "failed" can come out green. Values: observability/schema.yml.
STATUS_COLORS = {"succeeded": "green", "failed": "red", "retry": "orange", "cancelled": "gray"}


def status_series_colors() -> list[dict]:
    """One override per outcome, matched by pattern: depending on the Grafana
    version the series is named "failed", "Runs failed", or
    "Runs {Outcome=failed}". (No outcome's name contains another's.)"""
    return [{"matcher": {"id": "byRegexp", "options": f".*{status}.*"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}
            for status, color in STATUS_COLORS.items()]


def status_cells(column: str) -> dict:
    """A table column whose cells are coloured by outcome."""
    mapping = {status: {"color": color, "index": i} for i, (status, color) in enumerate(STATUS_COLORS.items())}
    return {"matcher": {"id": "byName", "options": column},
            "properties": [{"id": "mappings", "value": [{"type": "value", "options": mapping}]},
                           {"id": "custom.cellOptions", "value": {"type": "color-background"}}]}


def layout(panels: list[dict]) -> list[dict]:
    """Place panels left to right in rows of 24 columns (Grafana's grid)."""
    x = y = row_height = 0
    for number, p in enumerate(panels, start=1):
        width, height = p.pop("_size")
        if x + width > 24:
            x, y, row_height = 0, y + row_height, 0
        p["id"] = number
        p["gridPos"] = {"x": x, "y": y, "w": width, "h": height}
        x += width
        row_height = max(row_height, height)
    return panels


VARIABLES = [
    {"name": "azmon", "label": "Azure Monitor", "type": "datasource", "query": AZMON_TYPE, "hide": 0},
    {"name": "workspace", "label": "Log Analytics workspace resource ID", "type": "textbox", "hide": 0,
     "query": "/subscriptions/<subscription-id>/resourceGroups/rg-shoplite-prod/providers/"
              "Microsoft.OperationalInsights/workspaces/law-shoplite-prod"},
    {"name": "subscription", "label": "Subscription ID", "type": "textbox", "hide": 0,
     "query": "<subscription-id>"},
    {"name": "adx_cluster", "label": "ADX cluster URI", "type": "textbox", "hide": 0,
     "query": "https://<cluster>.<region>.kusto.windows.net"},
    {"name": "adx_database", "label": "ADX database", "type": "textbox", "hide": 0,
     "query": "shoplite"},
]


def dashboard(uid: str, title: str, description: str, panels: list[dict], time_from: str = "now-24h") -> dict:
    variables = []
    for v in VARIABLES:
        v = dict(v)
        if v["type"] == "textbox":
            v["current"] = {"text": v["query"], "value": v["query"]}
        variables.append(v)
    return {
        "uid": uid, "title": title, "description": description,
        "tags": ["shoplite", "phase5"], "timezone": "utc", "schemaVersion": 39, "version": 1,
        "editable": True, "refresh": "5m", "time": {"from": time_from, "to": "now"},
        "templating": {"list": variables}, "panels": layout(panels),
    }


# =============================================================================
# The dashboards
# =============================================================================
def overview() -> dict:
    return dashboard("shoplite-overview", "ShopLite: Overview",
                     "Is ShopLite healthy right now? The one dashboard to open first.", [
        stat("Collector: minutes since last heartbeat", la("""
            CollectorHeartbeat_CL | summarize Last = max(TimeGenerated)
            | project Minutes = datetime_diff('minute', now(), Last)"""), "m",
             description="Over 5 = the heartbeat alert fires (business hours).",
             thresholds=[(None, "green"), (5, "red")]),
        stat("API requests (last 15 min)", la("""
            AppRequests | where TimeGenerated > ago(15m) | summarize Requests = sum(ItemCount)""")),
        stat("API 5xx % (last 15 min)", la("""
            AppRequests | where TimeGenerated > ago(15m)
            | summarize Errors = sumif(ItemCount, ResultCode startswith "5"), Requests = sum(ItemCount)
            | project ErrorPercent = iff(Requests == 0, 0.0, 100.0 * Errors / Requests)"""), "percent",
             thresholds=[(None, "green"), (5, "red")]),
        stat("API p95 (last 15 min)", la("""
            AppRequests | where TimeGenerated > ago(15m) | summarize P95 = percentile(DurationMs, 95)"""), "ms",
             thresholds=[(None, "green"), (1500, "red")]),
        stat("Blocked sessions (last 5 min)", la("""
            SqlBlocking_CL | where TimeGenerated > ago(5m) and Role == "blocked"
            | summarize Blocked = dcount(SessionId)"""),
             thresholds=[(None, "green"), (1, "red")]),
        stat("SalesDB: minutes since log backup", la("""
            SqlDatabases_CL | where DatabaseName == "SalesDB" | summarize arg_max(TimeGenerated, LastLogBackupAt)
            | project Minutes = datetime_diff('minute', now(), LastLogBackupAt)"""), "m",
             thresholds=[(None, "green"), (30, "red")]),
        ts("API requests per minute, and server errors", la("""
            AppRequests | where $__timeFilter(TimeGenerated)
            | summarize Requests = sum(ItemCount), ServerErrors = sumif(ItemCount, ResultCode startswith "5")
                by bin(TimeGenerated, 1m)""")),
        ts("SQL Server batch requests per second", adx("""
            SqlPerfCounters_CL | where $__timeFilter(TimeGenerated) and CounterName == "Batch Requests/sec"
            | summarize BatchRequestsPerSec = avg(Value) by bin(TimeGenerated, $__timeInterval)""")),
        ts("VM CPU credits remaining", metric("CPU Credits Remaining"),
           description="Burstable VM: near zero = throttled. The alert fires below 30."),
        ts("What SQL Server waited on (seconds per interval, top 6)", adx("""
            let top = SqlWaitStats_CL | where $__timeFilter(TimeGenerated)
                | summarize Total = sum(WaitTimeMs) by WaitType | top 6 by Total | project WaitType;
            SqlWaitStats_CL | where $__timeFilter(TimeGenerated) and WaitType in (top)
            | summarize WaitSeconds = sum(WaitTimeMs) / 1000.0 by bin(TimeGenerated, $__timeInterval), WaitType
            | order by TimeGenerated asc"""), "s", stacked=True),
        table("Failed SQL Agent jobs (last 24 h)", la("""
            SqlAgentJobRuns_CL | where TimeGenerated > ago(24h) and Outcome == "failed"
            | summarize arg_max(TimeGenerated, JobName, FailedStepName, FailedStepMessage) by HistoryId
            | project TimeGenerated, JobName, Outcome, FailedStepName, FailedStepMessage | order by TimeGenerated desc""",
            table=True), height=6, status_column="Outcome"),
    ])


def sql_server() -> dict:
    return dashboard("shoplite-sql-server", "ShopLite: SQL Server",
                     "SQL Server internals over time (ADX history, via Log Analytics): waits, workload, sessions, blocking, "
                     "deadlocks, top queries, logs, and file I/O.", [
        ts("Waits by type (seconds per interval, top 8)", adx("""
            let top = SqlWaitStats_CL | where $__timeFilter(TimeGenerated)
                | summarize Total = sum(WaitTimeMs) by WaitType | top 8 by Total | project WaitType;
            SqlWaitStats_CL | where $__timeFilter(TimeGenerated) and WaitType in (top)
            | summarize WaitSeconds = sum(WaitTimeMs) / 1000.0 by bin(TimeGenerated, $__timeInterval), WaitType
            | order by TimeGenerated asc"""), "s", width=24, stacked=True,
           description="The best starting point for 'why is it slow?': LCK_* = blocking, PAGEIOLATCH_* = "
                       "disk reads, SOS_SCHEDULER_YIELD = CPU, WRITELOG = log writes."),
        ts("Workload (per second)", adx("""
            SqlPerfCounters_CL | where $__timeFilter(TimeGenerated)
            | where CounterName in ("Batch Requests/sec", "SQL Compilations/sec", "SQL Re-Compilations/sec")
            | summarize Value = avg(Value) by bin(TimeGenerated, $__timeInterval), CounterName
            | order by TimeGenerated asc""")),
        ts("Memory: page life expectancy and grants pending", adx("""
            SqlPerfCounters_CL | where $__timeFilter(TimeGenerated)
            | where CounterName in ("Page life expectancy", "Memory Grants Pending")
            | summarize Value = avg(Value) by bin(TimeGenerated, $__timeInterval), CounterName
            | order by TimeGenerated asc""")),
        ts("Sessions by program", adx("""
            SqlSessions_CL | where $__timeFilter(TimeGenerated)
            | summarize Sessions = max(Sessions) by bin(TimeGenerated, $__timeInterval), ProgramName
            | order by TimeGenerated asc"""), stacked=True),
        ts("Blocked sessions and deadlocks", adx("""
            union
              (SqlBlocking_CL | where $__timeFilter(TimeGenerated) and Role == "blocked"
               | summarize Value = dcount(SessionId) by bin(TimeGenerated, $__timeInterval) | extend Series = "blocked sessions"),
              (SqlXEvents_CL | where $__timeFilter(TimeGenerated) and EventName == "xml_deadlock_report"
               | summarize Value = dcount(EventId) by bin(TimeGenerated, $__timeInterval) | extend Series = "deadlocks")
            | order by TimeGenerated asc""")),
        table("Head blockers in the time range", adx("""
            SqlBlocking_CL | where $__timeFilter(TimeGenerated) and Role == "head_blocker"
            | summarize FirstSeen = min(TimeGenerated), LastSeen = max(TimeGenerated),
                        LongestTransactionSeconds = max(TransactionSeconds),
                        arg_max(TimeGenerated, ProgramName, LoginName, HostName, SessionStatus, StatementText)
                by SessionId
            | order by LastSeen desc""", table=True)),
        table("Top queries in the time range (Query Store, by total duration)", adx("""
            SqlTopQueries_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by EventId
            | summarize Executions = sum(Executions), TotalSeconds = round(sum(TotalDurationMs) / 1000, 1),
                        AvgMs = round(sum(TotalDurationMs) / sum(Executions), 1), MaxMs = max(MaxDurationMs),
                        Plans = dcount(PlanHash), ForcedPlan = max(toint(IsForcedPlan)),
                        QueryText = take_any(QueryText)
                by DatabaseName, QueryHash
            | top 20 by TotalSeconds desc""", table=True), height=10,
              description="Plans above 1 = the query ran with more than one plan: a possible plan regression. "
                          "ForcedPlan 1 = a plan is forced in Query Store: the usual suspect when it's slow."),
        ts("Transaction log used (%) by database", adx("""
            SqlDatabases_CL | where $__timeFilter(TimeGenerated) and isnotnull(LogUsedPercent)
            | summarize LogUsedPercent = max(LogUsedPercent) by bin(TimeGenerated, $__timeInterval), DatabaseName
            | order by TimeGenerated asc"""), "percent",
           description="The log-full alert fires above 70%."),
        ts("Average read and write latency by database file", adx("""
            SqlFileIo_CL | where $__timeFilter(TimeGenerated)
            | summarize ReadMs = sum(ReadStallMs) * 1.0 / iff(sum(Reads) == 0, 1, sum(Reads)),
                        WriteMs = sum(WriteStallMs) * 1.0 / iff(sum(Writes) == 0, 1, sum(Writes))
                by bin(TimeGenerated, $__timeInterval), File = strcat(DatabaseName, " ", FileType)
            | order by TimeGenerated asc"""), "ms"),
        ts("Database data size (MB)", adx("""
            SqlDatabases_CL | where $__timeFilter(TimeGenerated) and isnotnull(DataSizeMB)
            | summarize DataSizeMB = max(DataSizeMB) by bin(TimeGenerated, 1h), DatabaseName
            | order by TimeGenerated asc"""), "decmbytes", width=24),
    ])


def operations() -> dict:
    return dashboard("shoplite-operations", "ShopLite: Operations",
                     "Jobs, backups, restore tests, the vendor ETL, ReportingDB, maintenance, and the vendor "
                     "(ADX history, via Log Analytics).", [
        bars("Job runs per day by outcome", adx("""
            SqlAgentJobRuns_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by HistoryId
            | summarize Runs = count() by TimeGenerated = startofday(TimeGenerated), Outcome
            | order by TimeGenerated asc"""), status_series=True),
        ts("Minutes since SalesDB's last backups", adx("""
            SqlDatabases_CL | where $__timeFilter(TimeGenerated) and DatabaseName == "SalesDB"
            | summarize Log = max(datetime_diff('minute', TimeGenerated, LastLogBackupAt)),
                        Full = max(datetime_diff('minute', TimeGenerated, LastFullBackupAt)) / 60.0
                by bin(TimeGenerated, $__timeInterval)
            | project TimeGenerated, ['Log backup (minutes)'] = Log, ['Full backup (hours)'] = Full
            | order by TimeGenerated asc"""),
           description="The log backup line should saw-tooth under 15-20 minutes during business hours."),
        table("Job runs (newest first)", adx("""
            SqlAgentJobRuns_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by HistoryId
            | project TimeGenerated, JobName, Outcome, DurationSeconds, FailedStepName, FailedStepMessage
            | order by TimeGenerated desc | take 100""", table=True), height=10, status_column="Outcome"),
        ts("Backup size written (MB) by type", adx("""
            SqlBackups_CL | where $__timeFilter(TimeGenerated) and DatabaseName == "SalesDB" and not(IsCopyOnly)
            | summarize arg_max(TimeGenerated, *) by BackupSetId
            | summarize CompressedMB = sum(CompressedMB) by bin(TimeGenerated, 1h), BackupType
            | order by TimeGenerated asc"""), "decmbytes"),
        table("Restore tests", adx("""
            DbaRestoreTests_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by RunId
            | project TimeGenerated, Status, RestoreSeconds, CheckdbSeconds, LogBackupsRestored, RestoredTo, ErrorMessage
            | order by TimeGenerated desc""", table=True), width=12, status_column="Status",
              description="RestoreSeconds is the measured recovery time (RTO)."),
        table("Vendor ETL runs", adx("""
            OpsEtlRuns_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by RunId
            | project TimeGenerated, Status, DurationSeconds, RowsRead, RowsInserted, RowsUpdated,
                      RowsDiscontinued, RowsSkipped, ErrorStage, ErrorMessage
            | order by TimeGenerated desc""", table=True), width=12, status_column="Status"),
        ts("Vendor: freshness (hours) and connect time (ms)", adx("""
            VendorFeed_CL | where $__timeFilter(TimeGenerated)
            | summarize FreshnessHours = max(FreshnessHours), ConnectMs = max(ConnectMs)
                by bin(TimeGenerated, $__timeInterval)
            | order by TimeGenerated asc"""),
           description="The data contract allows 36 hours. ConnectMs spikes when Neon's free tier wakes up."),
        table("ReportingDB refreshes", adx("""
            OpsRefreshRuns_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by RunId
            | project TimeGenerated, Mode, Status, DurationSeconds, DaysRebuilt, RowsWritten, ErrorMessage
            | order by TimeGenerated desc""", table=True), width=12, status_column="Status"),
        bars("Maintenance time per day by command (minutes)", adx("""
            DbaCommandLog_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by CommandLogId
            | summarize Minutes = round(sum(DurationSeconds) / 60, 1) by TimeGenerated = startofday(TimeGenerated), CommandType
            | order by TimeGenerated asc"""), width=24),
    ], time_from="now-7d")


def api() -> dict:
    return dashboard("shoplite-api", "ShopLite: API",
                     "The ShopLite API from Application Insights (Log Analytics). Counts use sum(ItemCount) "
                     "because of sampling.", [
        ts("Requests per minute by source", la("""
            AppRequests | where $__timeFilter(TimeGenerated)
            | extend Source = tostring(Properties["shoplite.synthetic_source"])
            | extend Source = iff(Source in ("", "-"), "real (untagged)", Source)
            | summarize Requests = sum(ItemCount) by bin(TimeGenerated, 5m), Source
            | order by TimeGenerated asc"""), width=24, stacked=True,
           description="Tagged by the Playwright shoppers and Locust (X-Synthetic-Source)."),
        ts("Latency: median and p95", la("""
            AppRequests | where $__timeFilter(TimeGenerated)
            | summarize P50 = percentile(DurationMs, 50), P95 = percentile(DurationMs, 95) by bin(TimeGenerated, 5m)
            | order by TimeGenerated asc"""), "ms",
           description="The latency alert fires when p95 stays above 1.5 s."),
        ts("Server errors (%)", la("""
            AppRequests | where $__timeFilter(TimeGenerated)
            | summarize Errors = sumif(ItemCount, ResultCode startswith "5"), Requests = sum(ItemCount)
                by bin(TimeGenerated, 5m)
            | project TimeGenerated, ErrorPercent = iff(Requests == 0, 0.0, 100.0 * Errors / Requests)
            | order by TimeGenerated asc"""), "percent"),
        table("Endpoints in the time range", la("""
            AppRequests | where $__timeFilter(TimeGenerated)
            | summarize Requests = sum(ItemCount), P95Ms = round(percentile(DurationMs, 95)),
                        ServerErrors = sumif(ItemCount, ResultCode startswith "5"),
                        Status409 = sumif(ItemCount, ResultCode == "409")
                by Name
            | order by Requests desc""", table=True), width=12),
        table("Dependencies: SQL and the ranking service", la("""
            AppDependencies | where $__timeFilter(TimeGenerated)
            | summarize Calls = sum(ItemCount), AvgMs = round(avg(DurationMs), 1), P95Ms = round(percentile(DurationMs, 95)),
                        Failed = sumif(ItemCount, Success == false)
                by Type, Target
            | order by Calls desc""", table=True), width=12),
        ts("Slowest SQL statements (p95 ms, top 5)", la("""
            let top = AppDependencies | where $__timeFilter(TimeGenerated) and Type == "SQL"
                | summarize P95 = percentile(DurationMs, 95) by Data | top 5 by P95 | project Data;
            AppDependencies | where $__timeFilter(TimeGenerated) and Type == "SQL" and Data in (top)
            | summarize P95 = percentile(DurationMs, 95) by bin(TimeGenerated, 15m), Statement = substring(Data, 0, 60)
            | order by TimeGenerated asc"""), "ms", width=24),
        table("Exceptions (newest first)", la("""
            AppExceptions | where $__timeFilter(TimeGenerated)
            | project TimeGenerated, ExceptionType, OuterMessage, OperationName, OperationId
            | order by TimeGenerated desc | take 50""", table=True)),
    ])


def vm() -> dict:
    return dashboard("shoplite-vm", "ShopLite: VM",
                     "prod-sql-01: platform metrics, the Azure Monitor Agent's counters and events, and the "
                     "collector's own health.", [
        ts("CPU credits remaining", metric("CPU Credits Remaining"),
           description="The alert fires below 30. Near zero = the VM is throttled to its baseline."),
        ts("CPU (%)", metric("Percentage CPU"), "percent"),
        ts("Process CPU: SQL Server and Python (API, collector)", la("""
            Perf | where $__timeFilter(TimeGenerated) and ObjectName == "Process" and CounterName == "% Processor Time"
            | where InstanceName startswith "sqlservr" or InstanceName startswith "python"
            | summarize CpuPercent = avg(CounterValue) by bin(TimeGenerated, $__interval), InstanceName
            | order by TimeGenerated asc"""), "percent",
           description="Process CPU counts each core as 100%, so 2 cores busy = 200%."),
        ts("Memory: available MB and committed %", la("""
            Perf | where $__timeFilter(TimeGenerated) and ObjectName == "Memory"
            | summarize Value = avg(CounterValue) by bin(TimeGenerated, $__interval), CounterName
            | order by TimeGenerated asc""")),
        ts("Free disk space (%)", la("""
            Perf | where $__timeFilter(TimeGenerated) and ObjectName == "LogicalDisk" and CounterName == "% Free Space"
            | summarize FreePercent = avg(CounterValue) by bin(TimeGenerated, $__interval), InstanceName
            | order by TimeGenerated asc"""), "percent",
           description="F: is the data disk; the alert fires below 20% free."),
        ts("Data disk F: latency (ms)", la("""
            Perf | where $__timeFilter(TimeGenerated) and ObjectName == "LogicalDisk" and InstanceName == "F:"
            | where CounterName in ("Avg. Disk sec/Read", "Avg. Disk sec/Write")
            | summarize Ms = avg(CounterValue) * 1000 by bin(TimeGenerated, $__interval), CounterName
            | order by TimeGenerated asc"""), "ms"),
        ts("Collector: cycle time and buffered records", la("""
            CollectorHeartbeat_CL | where $__timeFilter(TimeGenerated)
            | summarize CycleSeconds = max(CycleSeconds), BufferedRecords = max(BufferedRecords)
                by bin(TimeGenerated, $__interval)
            | order by TimeGenerated asc"""),
           description="Buffered records above 0 = a destination is unreachable; data waits on F:."),
        table("Windows and SQL Server events (warnings and errors)", la("""
            Event | where $__timeFilter(TimeGenerated)
            | where EventLevelName in ("Error", "Warning") or Source == "MSSQLSERVER"
            | project TimeGenerated, EventLog, Source, EventID, EventLevelName, RenderedDescription
            | order by TimeGenerated desc | take 100""", table=True), width=12),
    ])


# -----------------------------------------------------------------------------
# The default run: import-ready copies with your values
# -----------------------------------------------------------------------------
# Dashboard variable -> (root .env variable, default if unset). None = required.
LOCAL_VALUES = {
    "azmon": ("GRAFANA_DATASOURCE", "Azure Monitor shoplite"),
    "workspace": ("GRAFANA_WORKSPACE_ID", None),
    "subscription": ("GRAFANA_SUBSCRIPTION_ID", None),
    "adx_cluster": ("GRAFANA_ADX_CLUSTER_URI", None),
    "adx_database": ("GRAFANA_ADX_DATABASE", "shoplite"),
}


def local_values(root_env: Path | None = None, environ=None) -> dict[str, str]:
    """Your values for the variables: real environment, then the root .env
    (layered, tools/envconfig.py). Exits naming every missing one: a half
    filled copy would import and then show errors panel by panel."""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from envconfig import ROOT_ENV, load_layers, setting
    load_layers(root_env=root_env or ROOT_ENV, environ=environ)
    values, missing = {}, []
    for variable, (env_name, default) in LOCAL_VALUES.items():
        value = (setting(env_name, default=default, environ=environ) or "").strip()
        if value and "<" not in value and value != "replace-me":     # still .env.example's placeholder?
            values[variable] = value
        else:
            missing.append(env_name)
    if missing:
        sys.exit(f"Set {', '.join(missing)} in the root .env (see .env.example), or run with --placeholders. "
                 "Nothing written.")
    return values


def with_values(board: dict, values: dict[str, str]) -> dict:
    """A copy of the dashboard whose variables default to your values."""
    board = copy.deepcopy(board)
    for variable in board["templating"]["list"]:
        value = values.get(variable["name"])
        if value is None:
            continue
        if variable["type"] == "textbox":
            variable["query"] = value
        variable["current"] = {"text": value, "value": value, "selected": True}
    return board


DASHBOARDS = (("overview", overview), ("sql-server", sql_server), ("operations", operations),
              ("api", api), ("vm", vm))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Grafana dashboards (import-ready, from the root .env)")
    parser.add_argument("--placeholders", action="store_true",
                        help="no .env: write <name>.placeholders.json to observability/grafana/placeholders/ instead")
    args = parser.parse_args()

    # Exactly one set per run, each in its own folder with its own file names.
    if args.placeholders:
        out, suffix, values = PLACEHOLDERS_OUT, ".placeholders.json", None
    else:
        out, suffix, values = LOCAL_OUT, ".json", local_values()     # fails before writing anything
    out.mkdir(parents=True, exist_ok=True)
    for name, build in DASHBOARDS:
        board = build()
        if values:
            board = with_values(board, values)
        path = out / f"{name}{suffix}"
        path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
        print(f"{path.relative_to(REPO_ROOT)}: {len(board['panels'])} panels")


if __name__ == "__main__":
    main()
