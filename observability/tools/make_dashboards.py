"""Generates the Grafana dashboards (observability/grafana/*.json) from the
compact definitions below: dashboards as code.

Writing Grafana's JSON by hand means hundreds of lines of boilerplate per
dashboard (panel positions, data source blocks, field settings), easy to break
and hard to review. Here each panel is one line or two: a title, a data
source, a KQL query, a unit. Change a panel here, re-run, re-import.

    python observability/tools/make_dashboards.py      (from the repo root, any Python 3)

Data sources, chosen per panel:
  ADX ("History")          the collector's tables, for the selected time range;
                           months of history; ingestion lags about 5 minutes
  Log Analytics ("Now")    near-real-time collector data, and what only lives
                           there: Application Insights (AppRequests, ...) and
                           the agent's Perf, Event, Heartbeat
  Azure metrics            the VM's platform metrics (CPU credits, CPU)

Every dashboard has the same variables, so no data source ID from your
Grafana stack is written into the files:
  adx, azmon      the two data sources (picked from a list after import)
  workspace       the Log Analytics workspace's resource ID
  subscription    the Azure subscription ID (for the VM metrics)

KQL macros the data sources fill in: $__timeFilter(TimeGenerated) (the
dashboard's time range) and $__timeInterval / $__interval (a bin size that
fits the panel's width). Event tables are de-duplicated by their unique_id
(at-least-once delivery, observability/schema.yml).
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT = REPO_ROOT / "observability" / "grafana"

ADX_TYPE = "grafana-azure-data-explorer-datasource"
AZMON_TYPE = "grafana-azure-monitor-datasource"
VM = {"resourceGroup": "rg-shoplite-prod", "resourceName": "prod-sql-01",
      "metricNamespace": "microsoft.compute/virtualmachines", "region": "northcentralus"}


# -----------------------------------------------------------------------------
# Targets (one query each)
# -----------------------------------------------------------------------------
def adx(kql: str, table: bool = False) -> dict:
    return {"refId": "A", "datasource": {"type": ADX_TYPE, "uid": "${adx}"}, "database": "shoplite",
            "queryType": "KQL", "rawMode": True, "query": kql.strip(),
            "resultFormat": "table" if table else "time_series"}


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
          description: str = "", options: dict | None = None, thresholds: list | None = None) -> dict:
    defaults = {"unit": unit}
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute",
                                  "steps": [{"color": color, "value": value} for value, color in thresholds]}
        defaults["color"] = {"mode": "thresholds"}
    return {"type": kind, "title": title, "description": description, "datasource": target["datasource"],
            "targets": [target], "fieldConfig": {"defaults": defaults, "overrides": []},
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


def table(title, target, width=24, height=8, description=""):
    return panel("table", title, target, width, height, "short", description, {"showHeader": True})


def bars(title, target, unit="short", width=12, description=""):
    """Stacked daily bars: a time series drawn as bars, one colour per series."""
    p = ts(title, target, unit, width, description)
    p["fieldConfig"]["defaults"]["custom"] = {"drawStyle": "bars", "fillOpacity": 80,
                                              "stacking": {"mode": "normal"}}
    return p


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
    {"name": "adx", "label": "History (ADX)", "type": "datasource", "query": ADX_TYPE, "hide": 0},
    {"name": "azmon", "label": "Now (Azure Monitor)", "type": "datasource", "query": AZMON_TYPE, "hide": 0},
    {"name": "workspace", "label": "Log Analytics workspace resource ID", "type": "textbox", "hide": 0,
     "query": "/subscriptions/<subscription-id>/resourceGroups/rg-shoplite-prod/providers/"
              "Microsoft.OperationalInsights/workspaces/law-shoplite-prod"},
    {"name": "subscription", "label": "Subscription ID", "type": "textbox", "hide": 0,
     "query": "<subscription-id>"},
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
            | project TimeGenerated, JobName, FailedStepName, FailedStepMessage | order by TimeGenerated desc""",
            table=True), height=6),
    ])


def sql_server() -> dict:
    return dashboard("shoplite-sql-server", "ShopLite: SQL Server",
                     "SQL Server internals over time (ADX history): waits, workload, sessions, blocking, "
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
                     "(ADX history).", [
        bars("Job runs per day by outcome", adx("""
            SqlAgentJobRuns_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by HistoryId
            | summarize Runs = count() by TimeGenerated = startofday(TimeGenerated), Outcome
            | order by TimeGenerated asc""")),
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
            | order by TimeGenerated desc | take 100""", table=True), height=10),
        ts("Backup size written (MB) by type", adx("""
            SqlBackups_CL | where $__timeFilter(TimeGenerated) and DatabaseName == "SalesDB" and not(IsCopyOnly)
            | summarize arg_max(TimeGenerated, *) by BackupSetId
            | summarize CompressedMB = sum(CompressedMB) by bin(TimeGenerated, 1h), BackupType
            | order by TimeGenerated asc"""), "decmbytes"),
        table("Restore tests", adx("""
            DbaRestoreTests_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by RunId
            | project TimeGenerated, Status, RestoreSeconds, CheckdbSeconds, LogBackupsRestored, RestoredTo, ErrorMessage
            | order by TimeGenerated desc""", table=True), width=12,
              description="RestoreSeconds is the measured recovery time (RTO)."),
        table("Vendor ETL runs", adx("""
            OpsEtlRuns_CL | where $__timeFilter(TimeGenerated)
            | summarize arg_max(TimeGenerated, *) by RunId
            | project TimeGenerated, Status, DurationSeconds, RowsRead, RowsInserted, RowsUpdated,
                      RowsDiscontinued, RowsSkipped, ErrorStage, ErrorMessage
            | order by TimeGenerated desc""", table=True), width=12),
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
            | order by TimeGenerated desc""", table=True), width=12),
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


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("overview", overview), ("sql-server", sql_server), ("operations", operations),
                        ("api", api), ("vm", vm)):
        board = build()
        (OUT / f"{name}.json").write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
        print(f"{name}.json: {len(board['panels'])} panels")


if __name__ == "__main__":
    main()
